import { FileImageOutlined } from '@ant-design/icons';
import { useEffect, useRef, useState } from 'react';
import { errorMessage } from '../api';
import type { MaterialLibraryApi } from '../material-library-api';
import type { LibraryMaterial } from '../material-library-types';

/** 接近视口才带认证读取小图；翻页、离开和迟到响应都释放请求与本地 URL。 */
export function MaterialThumbnail({api, asset, onOpen}: {
  api: MaterialLibraryApi; asset: LibraryMaterial; onOpen: () => void;
}) {
  const target = useRef<HTMLButtonElement>(null);
  const [url, setUrl] = useState(''), [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let owned = '', disposed = false, started = false;
    setUrl(''); setError('');
    const observer = new IntersectionObserver(entries => {
      if (disposed || started || !entries.some(entry => entry.isIntersecting)) return;
      started = true; observer.disconnect();
      api.thumbnail(asset.id, controller.signal).then(blob => {
        owned = URL.createObjectURL(blob);
        if (disposed) { URL.revokeObjectURL(owned); owned = ''; }
        else setUrl(owned);
      }).catch(failure => { if (!disposed) setError(errorMessage(failure)); });
    }, {rootMargin: '160px'});
    if (target.current) observer.observe(target.current);
    return () => {
      disposed = true; observer.disconnect(); controller.abort();
      if (owned) URL.revokeObjectURL(owned);
    };
  }, [api, asset.id]);

  return <button type="button" ref={target} className="material-thumbnail" aria-label={`预览${asset.name}`}
    onClick={onOpen} title={error || '查看原件'}>
    {url ? <img src={url} alt={`${asset.name}缩略图`} loading="lazy" decoding="async" />
      : <><FileImageOutlined aria-hidden="true" /><small>{error ? '预览失败' : '加载中'}</small></>}
  </button>;
}
