import { restAPIv1 } from '@/utils/api';
import classNames from 'classnames';
import React, { useEffect, useMemo, useState } from 'react';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/popover';

interface IImage extends React.ImgHTMLAttributes<HTMLImageElement> {
  id: string;
  t?: string | number;
  label?: string;
}

type ImageCacheItem = {
  count: number;
  objectUrl?: string;
  promise?: Promise<string>;
  timer?: ReturnType<typeof setTimeout>;
};

const imageCache = new Map<string, ImageCacheItem>();

export const buildDocumentImageUrl = (id: string, t?: string | number) => {
  const params = new URLSearchParams();

  if (t) {
    params.set('_t', String(t));
  }

  const query = params.toString();
  return `${restAPIv1}/documents/images/${id}${query ? `?${query}` : ''}`;
};

const fetchDocumentImage = (url: string) => {
  let item = imageCache.get(url);

  if (!item) {
    item = { count: 0 };
    imageCache.set(url, item);
  }
  if (item.timer) {
    clearTimeout(item.timer);
    item.timer = undefined;
  }
  item.count += 1;

  if (!item.promise) {
    item.promise = fetch(url)
      .then((response) => {
        if (!response.ok) {
          throw new Error(response.statusText);
        }
        return response.blob();
      })
      .then((blob) => {
        item.objectUrl = URL.createObjectURL(blob);
        return item.objectUrl;
      })
      .catch((error) => {
        imageCache.delete(url);
        throw error;
      });
  }

  return {
    promise: item.promise,
    release: () => {
      item.count -= 1;
      if (item.count <= 0) {
        item.timer = setTimeout(() => {
          if (item.count <= 0) {
            if (item.objectUrl) {
              URL.revokeObjectURL(item.objectUrl);
            }
            imageCache.delete(url);
          }
        }, 30000);
      }
    },
  };
};

export const useDocumentImageUrl = (id: string, t?: string | number) => {
  const directUrl = useMemo(() => buildDocumentImageUrl(id, t), [id, t]);
  const [imageUrl, setImageUrl] = useState('');

  useEffect(() => {
    let ignore = false;
    const { promise, release } = fetchDocumentImage(directUrl);

    promise
      .then((url) => {
        if (ignore) {
          return;
        }
        setImageUrl(url);
      })
      .catch(() => {
        if (!ignore) {
          setImageUrl('');
        }
      });

    return () => {
      ignore = true;
      release();
    };
  }, [directUrl]);

  return imageUrl;
};

const Image = ({ id, t, label, className, ...props }: IImage) => {
  const src = useDocumentImageUrl(id, t);
  const imageElement = (
    <img
      {...props}
      src={src || undefined}
      className={classNames('max-w-[45vw] max-h-[40wh] block', className)}
    />
  );

  if (!label) {
    return imageElement;
  }

  return (
    <div className="relative inline-block w-full">
      {imageElement}
      <div className="absolute bottom-2 right-2 bg-accent-primary text-white px-2 py-0.5 rounded-xl text-xs font-normal backdrop-blur-sm">
        {label}
      </div>
    </div>
  );
};

export default Image;

export const ImageWithPopover = ({ id }: { id: string }) => {
  return (
    <Popover>
      <PopoverTrigger>
        <Image id={id} className="max-h-[100px] inline-block"></Image>
      </PopoverTrigger>
      <PopoverContent>
        <Image id={id} className="max-w-[100px] object-contain"></Image>
      </PopoverContent>
    </Popover>
  );
};
