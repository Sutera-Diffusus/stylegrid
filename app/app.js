(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const sampleBase = '../samples/';
  const demoData = {
    grid: ['preview_bike_grid.jpg'],
    hybrid: ['preview_bike_mix.jpg'],
    posters: ['preview_bike_poster_v.jpg', 'preview_bike_poster_s.jpg', 'preview_bike_poster_h.jpg']
  };
  const SPRING = 'cubic-bezier(.32,.72,0,1)';
  let selectedPreset = '深海蓝调';
  let selectedGallery = 'grid';
  let selectedFile = null;
  let remoteAssets = null;
  let latestDownload = '';
  let generationTimer = null;
  let toastTimer = null;
  let lbSources = [];
  let lbIndex = -1;
  let dragDepth = 0;
  let customStyles = [];
  let stylePoolLoaded = false;

  async function loadStylePool() {
    if (stylePoolLoaded || location.protocol === 'file:') return;
    const pool = $('#style-pool');
    try {
      const response = await fetch('/api/catalog');
      if (!response.ok) throw new Error();
      const { styles } = await response.json();
      pool.replaceChildren();
      styles.forEach(({ key, name }) => {
        const chip = document.createElement('button');
        chip.className = 'pool-chip';
        chip.textContent = name;
        chip.title = key;
        chip.addEventListener('click', () => {
          const key_on = chip.classList.toggle('selected');
          customStyles = key_on ? [...customStyles, key] : customStyles.filter(k => k !== key);
          $('#custom-styles-count').textContent = customStyles.length ? `已选 ${customStyles.length} 种 · 至多取前 8` : '未选择';
        });
        pool.append(chip);
      });
      stylePoolLoaded = true;
    } catch {
      pool.innerHTML = '<span class="pool-empty">风格目录加载失败 · 演示模式不可用</span>';
    }
  }

  function notify(message) {
    $('#toast-text').textContent = message;
    $('#toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 2200);
  }

  function timeAgo(ts) {
    const seconds = Math.max(0, (Date.now() / 1000 - ts) | 0);
    if (seconds < 60) return '刚刚';
    if (seconds < 3600) return `${(seconds / 60) | 0} 分钟前`;
    if (seconds < 86400) return `${(seconds / 3600) | 0} 小时前`;
    if (seconds < 172800) return '昨天';
    return `${(seconds / 86400) | 0} 天前`;
  }

  function assetsFromMap(assets) {
    return {
      grid: assets['01_九宫格.png'] ? [assets['01_九宫格.png']] : [],
      hybrid: assets['02_混血格.png'] ? [assets['02_混血格.png']] : [],
      posters: Object.entries(assets).filter(([name]) => name.startsWith('03_海报_')).map(([, url]) => url),
    };
  }

  // ——— 图片 blur-up ———
  function watchImg(img) {
    if (img.complete && img.naturalWidth) img.classList.add('img-loaded');
    else img.addEventListener('load', () => img.classList.add('img-loaded'), { once: true });
  }

  function resetImg(img) {
    img.classList.remove('img-loaded');
    watchImg(img);
  }

  // ——— FLIP 飞行动画(空间连续性转场)———
  function flyImage(imgEl, fromRect, toRect, { duration = 560, onDone } = {}) {
    const clone = imgEl.cloneNode();
    Object.assign(clone.style, {
      position: 'fixed', margin: '0', zIndex: '90', pointerEvents: 'none',
      left: `${fromRect.left}px`, top: `${fromRect.top}px`,
      width: `${fromRect.width}px`, height: `${fromRect.height}px`,
      objectFit: 'cover',
      borderRadius: getComputedStyle(imgEl).borderRadius,
      boxShadow: '0 18px 50px rgba(0,0,0,.25)',
      opacity: '1', filter: 'none',
      transition: `left ${duration}ms ${SPRING}, top ${duration}ms ${SPRING}, width ${duration}ms ${SPRING}, height ${duration}ms ${SPRING}, border-radius ${duration}ms ${SPRING}`,
    });
    document.body.append(clone);
    void clone.offsetWidth;
    Object.assign(clone.style, {
      left: `${toRect.left}px`, top: `${toRect.top}px`,
      width: `${toRect.width}px`, height: `${toRect.height}px`,
      borderRadius: '20px',
    });
    let finished = false;
    const done = () => { if (finished) return; finished = true; clone.remove(); onDone?.(); };
    clone.addEventListener('transitionend', done, { once: true });
    setTimeout(done, duration + 150);
  }

  function flyBlock(fromRect, toRect, { duration = 560 } = {}) {
    const block = document.createElement('div');
    Object.assign(block.style, {
      position: 'fixed', zIndex: '90', pointerEvents: 'none',
      left: `${fromRect.left}px`, top: `${fromRect.top}px`,
      width: `${fromRect.width}px`, height: `${fromRect.height}px`,
      borderRadius: '30px', background: 'var(--card)',
      boxShadow: 'var(--shadow-photo)',
      transition: `all ${duration}ms ${SPRING}`,
    });
    document.body.append(block);
    void block.offsetWidth;
    Object.assign(block.style, {
      left: `${toRect.left}px`, top: `${toRect.top}px`,
      width: `${toRect.width}px`, height: `${toRect.height}px`,
      borderRadius: '26px', opacity: '0',
    });
    setTimeout(() => block.remove(), duration + 100);
  }

  // ——— 状态机 ———
  function enterReady(flight = false) {
    const dz = $('#dropzone');
    const ready = $('#ready');
    let from = null;
    if (flight && !dz.hidden && dz.getBoundingClientRect().width) from = dz.getBoundingClientRect();
    dz.hidden = true;
    ready.hidden = false;
    if (from) {
      requestAnimationFrame(() => requestAnimationFrame(() => {
        const to = $('#source-image').getBoundingClientRect();
        if (to.width) flyBlock(from, to);
      }));
    }
  }

  function enterEmpty() {
    const image = $('#source-image');
    if (image.dataset.objectUrl) {
      URL.revokeObjectURL(image.dataset.objectUrl);
      delete image.dataset.objectUrl;
    }
    selectedFile = null;
    resetOutput();
    $('#ready').hidden = true;
    $('#dropzone').hidden = false;
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function resetOutput() {
    clearTimeout(generationTimer);
    latestDownload = '';
    remoteAssets = null;
    $('#results').hidden = true;
    $('#progress-stage').hidden = true;
    $('#gallery-stage').hidden = true;
    $('#progress-fill').style.width = '0';
    $('#progress-number').textContent = '0%';
    $('#generate-button').disabled = false;
    $('#generate-button').querySelector('.button-label').textContent = '生成整套作品';
    $('#score-pill').hidden = true;
    $('#score-big').textContent = '—';
    $('#output-count').textContent = '';
  }

  function setProgress(percent, title, sub) {
    $('#progress-number').textContent = `${percent}%`;
    $('#progress-fill').style.width = `${percent}%`;
    const cells = $$('#progress-grid i');
    const lit = Math.round(percent / 100 * cells.length);
    cells.forEach((cell, i) => cell.classList.toggle('on', i < lit));
    $('#progress-title').textContent = title;
    $('#progress-sub').textContent = sub;
  }

  function scrollToResults(behavior = 'smooth') {
    setTimeout(() => $('#results').scrollIntoView({ behavior, block: 'start' }), 80);
  }

  // ——— 素材 ———
  function setSource(file) {
    if (!file || !file.type.startsWith('image/')) {
      notify('请选择 PNG、JPG 或 WEBP 图片');
      return;
    }
    if (file.size > 20 * 1024 * 1024) {
      notify('图片超过 20 MB，请换一张素材');
      return;
    }
    selectedFile = file;
    resetOutput();
    const image = $('#source-image');
    if (image.dataset.objectUrl) URL.revokeObjectURL(image.dataset.objectUrl);
    const url = URL.createObjectURL(file);
    image.dataset.objectUrl = url;
    resetImg(image);
    image.src = url;
    $('#source-name').textContent = file.name;
    $('#source-size').textContent = `${(file.size / 1024).toFixed(file.size > 1024 * 1024 ? 0 : 1)} KB · 本地素材`;
    enterReady(true);
    if (location.protocol === 'file:') notify('静态文件模式只展示仓库样片；启动 server.py 可真实生成上传图');
  }

  function useSample() {
    selectedFile = null;
    resetOutput();
    const image = $('#source-image');
    if (image.dataset.objectUrl) {
      URL.revokeObjectURL(image.dataset.objectUrl);
      delete image.dataset.objectUrl;
    }
    resetImg(image);
    image.src = `${sampleBase}hero.jpg`;
    $('#source-name').textContent = '城市骑行 · 示例素材';
    $('#source-size').textContent = '本地样片 · 378 KB';
    $('#title-input').value = '风从街角经过';
    $('#subtitle-input').value = 'A ride through the blue hour';
    updateCount();
    enterReady(true);
    notify('已载入「城市骑行」示例');
  }

  function updateCount() {
    $('#char-count').textContent = `${$('#title-input').value.length} / 40`;
  }

  // ——— 画廊 ———
  function openLightbox(sources, index) {
    lbSources = sources;
    lbIndex = index;
    const box = $('#lightbox');
    const img = $('#lightbox-img');
    img.src = sources[index].src;
    $('#lb-glow').style.backgroundImage = `url("${sources[index].src}")`;
    box.hidden = false;
    void box.offsetWidth;
    box.classList.add('open');
    const fly = () => {
      const from = sources[index].getBoundingClientRect();
      const to = img.getBoundingClientRect();
      if (!from.width || !to.width) return;
      img.style.visibility = 'hidden';
      flyImage(img, from, to, { onDone: () => { img.style.visibility = ''; } });
    };
    if (img.complete && img.naturalWidth) fly();
    else img.addEventListener('load', fly, { once: true });
  }

  function closeLightbox() {
    const box = $('#lightbox');
    const img = $('#lightbox-img');
    if (box.hidden) return;
    box.classList.remove('open');
    const target = lbSources[lbIndex];
    const restore = () => { box.hidden = true; img.style.visibility = ''; };
    if (target && document.contains(target)) {
      const from = img.getBoundingClientRect();
      const to = target.getBoundingClientRect();
      if (from.width && to.width) {
        img.style.visibility = 'hidden';
        flyImage(img, from, to, { onDone: restore });
        setTimeout(restore, 750); // 兜底
        return;
      }
    }
    setTimeout(restore, 380);
  }

  function stepLightbox(delta) {
    if (!lbSources.length) return;
    lbIndex = (lbIndex + delta + lbSources.length) % lbSources.length;
    $('#lightbox-img').src = lbSources[lbIndex].src;
    $('#lb-glow').style.backgroundImage = `url("${lbSources[lbIndex].src}")`;
  }

  function buildGallery(tab = selectedGallery) {
    selectedGallery = tab;
    const content = $('#gallery-content');
    content.replaceChildren();
    const layout = document.createElement('div');
    layout.className = `gallery-layout ${tab === 'grid' ? 'grid-layout' : tab === 'hybrid' ? 'hybrid-layout' : 'poster-layout'}`;
    const files = remoteAssets ? remoteAssets[tab] : demoData[tab];
    const selectedFormats = $$('.format-card.selected').map(el => el.dataset.format.toLowerCase());
    const visible = tab === 'posters'
      ? files.filter((file) => remoteAssets
          ? selectedFormats.some((format) => file.includes(`海报_${format.toUpperCase()}`))
          : selectedFormats.some((format) => file.includes(`poster_${format}`)))
      : files;
    if (tab === 'posters' && !selectedFormats.length) {
      const empty = document.createElement('p');
      empty.className = 'gallery-empty';
      empty.textContent = '请选择至少一个输出比例';
      content.append(empty);
    } else {
      const sources = [];
      (visible.length ? visible : files).forEach((file, index) => {
        const image = document.createElement('img');
        watchImg(image);
        image.src = remoteAssets ? file : `${sampleBase}${file}`;
        image.alt = `${tab === 'grid' ? '九宫格' : tab === 'hybrid' ? '混合构图' : '海报变体'} · ${index + 1}`;
        image.className = 'gallery-image';
        image.loading = 'lazy';
        image.addEventListener('click', () => openLightbox(sources, sources.indexOf(image)));
        const frame = document.createElement('span');
        frame.className = 'g-frame';
        frame.addEventListener('pointermove', (event) => {
          const r = frame.getBoundingClientRect();
          frame.style.setProperty('--mx', `${((event.clientX - r.left) / r.width * 100).toFixed(1)}%`);
          frame.style.setProperty('--my', `${((event.clientY - r.top) / r.height * 100).toFixed(1)}%`);
        });
        frame.append(image);
        layout.append(frame);
        sources.push(image);
      });
      content.append(layout);
    }
    $$('.gallery-tab').forEach(button => button.classList.toggle('active', button.dataset.gallery === tab));
    moveSegThumb();
  }

  function moveSegThumb() {
    const active = $('.gallery-tab.active');
    const thumb = $('.seg-thumb');
    if (!active || !thumb) return;
    thumb.style.width = `${active.offsetWidth}px`;
    thumb.style.transform = `translateX(${active.offsetLeft}px)`;
  }

  function showGallery() {
    $('#results').hidden = false;
    $('#progress-stage').hidden = true;
    $('#gallery-stage').hidden = false;
    buildGallery(selectedGallery);
    requestAnimationFrame(moveSegThumb);
  }

  // ——— 生成 ———
  async function generateRemote() {
    $('#results').hidden = false;
    $('#gallery-stage').hidden = true;
    $('#progress-stage').hidden = false;
    $('#generate-button').disabled = true;
    setProgress(18, '正在读取构图关系…', '上传至本机进程 · 不离开设备');
    scrollToResults();
    const body = new FormData();
    body.append('file', selectedFile);
    body.append('preset', selectedPreset);
    body.append('blend', $('#mix-select').value);
    body.append('styles', selectedPreset === '自定义' ? customStyles.join(',') : '');
    body.append('density', $('#density-range').value);
    body.append('formats', $$('.format-card.selected').map(card => card.dataset.format).join(','));
    body.append('title', $('#title-input').value);
    body.append('sub', $('#subtitle-input').value);
    try {
      const response = await fetch('/api/generate', { method: 'POST', body });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '本地生成失败');
      latestDownload = result.download || '';
      remoteAssets = assetsFromMap(result.assets);
      const count = result.output_count || 0;
      const qaCount = result.qa_count || Object.keys(result.scores || {}).length;
      $('#output-count').textContent = `${count} 份产物 · ${qaCount} 项质检`;
      if (qaCount) {
        const average = Object.values(result.scores || {}).reduce((sum, value) => sum + Number(value), 0) / qaCount;
        $('#score-big').textContent = (average * 10).toFixed(1);
        $('#score-pill').hidden = false;
      }
      setProgress(100, '整套作品已经展开。', `本地引擎完成 · ${result.seconds}s`);
      setTimeout(() => {
        $('#generate-button').disabled = false;
        $('#generate-button').querySelector('.button-label').textContent = '重新生成整套作品';
        showGallery();
        scrollToResults();
        notify('本地工作流完成 · 产物已落盘');
        loadRecents();
      }, 350);
    } catch (error) {
      $('#generate-button').disabled = false;
      $('#generate-button').querySelector('.button-label').textContent = '生成整套作品';
      $('#progress-stage').hidden = true;
      $('#results').hidden = true;
      notify(error.message || '本地生成失败，已保留演示模式');
    }
  }

  function generate() {
    if ($$('.format-card.selected').length === 0) {
      notify('请至少选择一种输出格式');
      return;
    }
    if (location.protocol !== 'file:' && selectedFile) {
      generateRemote();
      return;
    }
    clearTimeout(generationTimer);
    $('#results').hidden = false;
    $('#gallery-stage').hidden = true;
    $('#progress-stage').hidden = false;
    $('#generate-button').disabled = true;
    $('#generate-button').querySelector('.button-label').textContent = '正在生成…';
    setProgress(12, '正在读取构图关系…', '内置样片预览 · 启动 server.py 可真实生成');
    scrollToResults();
    const stages = [
      [36, '正在读取构图关系…', '内置样片预览 · 本地演示', 650],
      [68, '正在混合视觉语言…', `「${selectedPreset}」× ${$('#mix-select').value} · 演示`, 1500],
      [100, '整套作品已经展开。', '内置样片演示已就绪 · 未调用工作流', 2400]
    ];
    stages.forEach(([percent, title, sub, delay], index) => {
      setTimeout(() => {
        if ($('#progress-stage').hidden) return;
        setProgress(percent, title, sub);
        if (index === stages.length - 1) {
          generationTimer = setTimeout(() => {
            $('#generate-button').disabled = false;
            $('#generate-button').querySelector('.button-label').textContent = '重新生成整套作品';
            $('#output-count').textContent = '演示产物 · 内置样片';
            showGallery();
            scrollToResults();
            notify('演示完成 · 使用仓库内置样片；真实上传请启动 server.py');
          }, 500);
        }
      }, delay);
    });
  }

  // ——— 最近作品 ———
  async function loadRecents() {
    if (location.protocol === 'file:') return;
    try {
      const response = await fetch('/api/recent');
      if (!response.ok) return;
      const { jobs } = await response.json();
      if (!jobs || !jobs.length) return;
      const row = $('#recent-row');
      row.replaceChildren();
      jobs.forEach((job, index) => {
        const card = document.createElement('button');
        card.className = 'recent-card';
        card.style.animationDelay = `${index * 60}ms`;
        const image = document.createElement('img');
        watchImg(image);
        image.src = job.cover;
        image.alt = `最近作品 · ${timeAgo(job.mtime)}`;
        image.draggable = false; // 防止原生图片拖拽吃掉 pointer 事件
        image.loading = 'lazy';
        const caption = document.createElement('span');
        caption.textContent = timeAgo(job.mtime);
        card.append(image, caption);
        card.addEventListener('click', () => {
          const from = image.getBoundingClientRect();
          remoteAssets = assetsFromMap(job.assets);
          latestDownload = `/api/download?job=${job.job}`;
          selectedGallery = 'grid';
          $('#score-pill').hidden = true;
          $('#output-count').textContent = '历史作品';
          showGallery();
          scrollToResults('auto');
          requestAnimationFrame(() => requestAnimationFrame(() => {
            const target = $('#gallery-content img');
            if (!target) return;
            const to = target.getBoundingClientRect();
            if (!from.width || !to.width) return;
            target.style.visibility = 'hidden';
            flyImage(image, from, to, { onDone: () => { target.style.visibility = ''; } });
          }));
          notify('已载入最近作品');
        });
        row.append(card);
      });
      const recents = $('#recents');
      recents.hidden = false;
      if ('IntersectionObserver' in window) {
        const io = new IntersectionObserver((entries) => {
          entries.forEach(entry => {
            if (entry.isIntersecting) { recents.classList.add('in-view'); io.disconnect(); }
          });
        }, { threshold: 0.12 });
        io.observe(recents);
      } else {
        recents.classList.add('in-view');
      }
    } catch { /* 无历史记录时保持静默 */ }
  }

  // ——— 事件 ———
  $('#file-input').addEventListener('change', event => setSource(event.target.files[0]));
  $$('[data-use-sample]').forEach(button => button.addEventListener('click', () => useSample()));
  // ——— Hero 作品光墙:真实产物做氛围,内容即科技感 ———
  (function buildWall() {
    const wall = $('#hero-wall');
    if (!wall) return;
    const pools = [
      ['preview_bike_grid.jpg', 'preview_moon_poster_v.jpg', 'preview_bike_mix.jpg'],
      ['preview_moon_grid.jpg', 'preview_bike_poster_s.jpg', 'preview_moon_poster_h.jpg'],
      ['preview_bike_poster_h.jpg', 'preview_moon_mix.jpg', 'preview_bike_poster_v.jpg'],
      ['preview_moon_poster_s.jpg', 'preview_bike_grid.jpg', 'preview_moon_grid.jpg'],
      ['preview_bike_mix.jpg', 'preview_moon_poster_h.jpg', 'preview_bike_poster_s.jpg'],
    ];
    pools.forEach((pool) => {
      const col = document.createElement('div');
      col.className = 'wall-col';
      const track = document.createElement('div');
      track.className = 'wall-track';
      [...pool, ...pool].forEach((name) => { // 复制一份,translateY(-50%) 无缝循环
        const img = document.createElement('img');
        img.src = `${sampleBase}${name}`;
        img.alt = '';
        img.loading = 'lazy';
        track.append(img);
      });
      col.append(track);
      wall.append(col);
    });
  })();

  $('#replace-source').addEventListener('click', () => $('#file-input').click());
  const dropzone = $('#dropzone');
  dropzone.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('#file-input').click(); }
  });
  ['dragenter', 'dragover'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.add('drag-over'); }));
  ['dragleave', 'drop'].forEach(type => dropzone.addEventListener(type, event => { event.preventDefault(); dropzone.classList.remove('drag-over'); }));

  // 全窗口拖放:任何位置松手即替换素材
  const hasFiles = (event) => [...(event.dataTransfer?.types || [])].includes('Files');
  window.addEventListener('dragenter', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth += 1;
    document.body.classList.add('dragging');
  });
  window.addEventListener('dragover', event => { if (hasFiles(event)) event.preventDefault(); });
  window.addEventListener('dragleave', event => {
    if (!hasFiles(event)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) document.body.classList.remove('dragging');
  });
  window.addEventListener('drop', event => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    dragDepth = 0;
    document.body.classList.remove('dragging');
    const file = event.dataTransfer.files?.[0];
    if (file) setSource(file);
  });

  $$('.style-card[data-preset]').forEach(card => card.addEventListener('click', () => {
    $$('.style-card[data-preset]').forEach(item => item.classList.toggle('selected', item === card));
    selectedPreset = card.dataset.preset;
    const isCustom = selectedPreset === '自定义';
    $('#custom-styles-field').hidden = !isCustom;
    if (isCustom) {
      $('#options').open = true;
      loadStylePool();
    }
  }));
  $$('.format-card').forEach(card => card.addEventListener('click', () => {
    card.classList.toggle('selected');
    if (!$('#gallery-stage').hidden && selectedGallery === 'posters') buildGallery('posters');
  }));
  $('#density-range').addEventListener('input', event => {
    $('#density-value').textContent = ['克制', '均衡', '丰沛'][Number(event.target.value) - 1];
  });
  $('#title-input').addEventListener('input', updateCount);
  $('#title-input').addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') generate();
  });
  $$('.gallery-tab').forEach(tab => tab.addEventListener('click', () => buildGallery(tab.dataset.gallery)));
  $('#generate-button').addEventListener('click', generate);
  $('#clear-output').addEventListener('click', () => {
    resetOutput();
    notify('已清空结果');
  });
  $('#new-creation').addEventListener('click', enterEmpty);
  $('#download-all').addEventListener('click', () => {
    if (latestDownload) {
      const link = document.createElement('a');
      link.href = latestDownload;
      link.download = 'stylegrid-output.zip';
      link.click();
      notify('已准备完整作品包下载');
      return;
    }
    const images = $$('#gallery-content img');
    if (!images.length) {
      notify('先生成一组作品，再下载预览');
      return;
    }
    images.forEach((image, index) => {
      const link = document.createElement('a');
      link.href = image.src;
      const extension = image.src.match(/\.(jpe?g|png|webp)(?:$|\?)/i)?.[1] || 'png';
      link.download = `stylegrid-${selectedGallery}-${index + 1}.${extension}`;
      link.click();
    });
    notify(`已准备 ${images.length} 张 ${selectedGallery === 'posters' ? '海报' : '预览'} 下载`);
  });
  $('.stage-action').addEventListener('click', () => {
    const first = $('#gallery-content img');
    if (first) window.open(first.src, '_blank', 'noopener');
    else notify('先生成一组作品，再打开大图');
  });
  $('#lightbox').addEventListener('click', closeLightbox);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closeLightbox(); return; }
    if ($('#lightbox').hidden) return;
    if (event.key === 'ArrowRight') stepLightbox(1);
    if (event.key === 'ArrowLeft') stepLightbox(-1);
  });
  window.addEventListener('scroll', () => {
    $('#topbar').classList.toggle('scrolled', window.scrollY > 8);
    const y = Math.min(window.scrollY, 480);
    const hero = $('.hero');
    hero.style.transform = `translateY(${y * -0.14}px)`;
    hero.style.opacity = `${1 - y / 620}`;
  }, { passive: true });
  window.addEventListener('resize', moveSegThumb);

  // ——— 光墙鼠标视差(lerp 平滑,反向漂移)———
  (function wallParallax() {
    const hero = $('.hero');
    const wall = $('#hero-wall');
    if (!hero || !wall) return;
    let targetX = 0, targetY = 0, curX = 0, curY = 0, rafId = null;
    const tick = () => {
      curX += (targetX - curX) * 0.07;
      curY += (targetY - curY) * 0.07;
      wall.style.transform = `translate3d(${curX.toFixed(2)}px, ${curY.toFixed(2)}px, 0)`;
      if (Math.abs(targetX - curX) > 0.05 || Math.abs(targetY - curY) > 0.05) rafId = requestAnimationFrame(tick);
      else rafId = null;
    };
    const wake = () => { if (!rafId) rafId = requestAnimationFrame(tick); };
    hero.addEventListener('pointermove', (event) => {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const rect = hero.getBoundingClientRect();
      targetX = ((event.clientX - rect.left) / rect.width - 0.5) * -20;
      targetY = ((event.clientY - rect.top) / rect.height - 0.5) * -12;
      wake();
    });
    hero.addEventListener('pointerleave', () => { targetX = 0; targetY = 0; wake(); });
  })();

  // ——— 素材照片反射光斑 ———
  (function photoShine() {
    const photo = $('.hero-photo');
    photo.addEventListener('pointermove', (event) => {
      const r = photo.getBoundingClientRect();
      photo.style.setProperty('--mx', `${((event.clientX - r.left) / r.width * 100).toFixed(1)}%`);
      photo.style.setProperty('--my', `${((event.clientY - r.top) / r.height * 100).toFixed(1)}%`);
    });
  })();

  // ——— 最近作品:拖拽滚动 + 可收起 ———
  (function recentsInteractions() {
    const recents = $('#recents');
    const row = $('#recent-row');
    const toggle = $('#recents-toggle');
    if (localStorage.getItem('sg-recents-collapsed') === '1') recents.classList.add('collapsed');
    toggle.addEventListener('click', () => {
      const collapsed = recents.classList.toggle('collapsed');
      toggle.setAttribute('aria-label', collapsed ? '展开最近作品' : '收起最近作品');
      localStorage.setItem('sg-recents-collapsed', collapsed ? '1' : '0');
    });
    let isDown = false, startX = 0, startScroll = 0, moved = 0;
    row.addEventListener('pointerdown', (event) => {
      isDown = true; moved = 0;
      startX = event.clientX;
      startScroll = row.scrollLeft;
      row.setPointerCapture(event.pointerId);
      row.classList.add('dragging');
    });
    row.addEventListener('pointermove', (event) => {
      if (!isDown) return;
      const dx = event.clientX - startX;
      moved = Math.max(moved, Math.abs(dx));
      row.scrollLeft = startScroll - dx;
    });
    ['pointerup', 'pointercancel'].forEach(type => row.addEventListener(type, () => {
      isDown = false;
      row.classList.remove('dragging');
    }));
    // 拖动超过阈值时拦截卡片点击,避免误触
    row.addEventListener('click', (event) => {
      if (moved > 6) { event.preventDefault(); event.stopPropagation(); moved = 0; }
    }, true);
    // 鼠标滚轮纵向滚动映射为横向
    row.addEventListener('wheel', (event) => {
      if (row.scrollWidth <= row.clientWidth) return;
      if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
      event.preventDefault();
      row.scrollLeft += event.deltaY;
    }, { passive: false });
  })();

  // ——— 落点 3D 微倾斜 ———
  (function dropzoneTilt() {
    const dz = $('#dropzone');
    dz.addEventListener('pointermove', (event) => {
      if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      const rect = dz.getBoundingClientRect();
      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;
      dz.style.transform = `perspective(900px) rotateX(${(-y * 3).toFixed(2)}deg) rotateY(${(x * 4).toFixed(2)}deg) translateY(-3px)`;
    });
    dz.addEventListener('pointerleave', () => { dz.style.transform = ''; });
  })();

  updateCount();
  loadRecents();
})();
