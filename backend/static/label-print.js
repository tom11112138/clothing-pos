(() => {
  const params = new URLSearchParams(location.search);
  const skuId = Number(params.get('sku_id'));
  const copies = Number(params.get('copies') || 1);
  const basis = params.get('price_basis') || 'tag';
  const status = document.getElementById('status');
  const print = document.getElementById('print');
  const download = document.getElementById('download');
  let label;
  const save = () => {
    if (!label) return;
    const link = document.createElement('a');
    link.href = label.image_url;
    link.download = `sku-${skuId}-40x60mm.png`;
    link.click();
  };
  print.addEventListener('click', () => { if (label && !print.disabled) window.print(); });
  download.addEventListener('click', save);
  const load = async () => {
    try {
      if (!Number.isSafeInteger(skuId) || skuId <= 0 || !Number.isSafeInteger(copies) || copies < 1 || copies > 100 || !['tag', 'selling'].includes(basis)) {
        throw new Error('商品或打印份数不正确');
      }
      const response = await fetch(`/api/skus/${skuId}/label?price_basis=${basis}`, { credentials:'same-origin' });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(response.status === 401 ? '请先在收银系统登录，再打开标签页面' : error.detail || '标签生成失败');
      }
      const data = await response.json();
      if (!data.printable) throw new Error((data.issues || []).join('；') || '合格证资料尚未确认');
      const image = new Image();
      image.src = data.image_url;
      await image.decode();
      const labels = document.getElementById('labels');
      for (let i = 0; i < copies; i += 1) {
        const item = document.createElement('figure');
        item.className = 'label';
        const copy = image.cloneNode();
        copy.alt = data.product_name;
        item.appendChild(copy);
        labels.appendChild(item);
      }
      label = data;
      document.title = `${data.product_name} · 40×60 mm`;
      status.textContent = `${copies} 张 · ${data.price_title} ¥${data.price} · 纸张 40×60 mm，比例 100%，页边距 0。B21 免驱软件请使用下载的标签图片；系统打印需要已安装的打印驱动。`;
      print.disabled = false;
      download.disabled = false;
    } catch (error) { status.textContent = error.message; }
  };
  load();
})();
