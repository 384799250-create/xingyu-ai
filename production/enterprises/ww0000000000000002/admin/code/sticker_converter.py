import base64,io,json,sys
def convert_sticker_webp(raw):
    """Convert bounded WebP to PNG/GIF without silently losing animation."""
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != 'WEBP':
                raise ValueError('请选择有效的 WebP 图片，不能只修改文件后缀')
            width, height = source.size
            count = getattr(source, 'n_frames', 1)
            if width * height > 8_000_000 or count > 120 or width * height * count > 32_000_000:
                raise ValueError('WebP 的像素或动画帧数过多，请缩小图片或缩短动画后重试')
            frames, durations = [], []
            loop = source.info.get('loop', 1)
            for index in range(count):
                source.seek(index)
                frame = source.convert('RGBA')
                frame.thumbnail((512, 512), Image.Resampling.LANCZOS)
                frames.append(frame)
                durations.append(max(10, round(source.info.get('duration', 100) / 10) * 10))
            if sum(durations) > 60_000:
                raise ValueError('WebP 动画超过 60 秒，请缩短后重试')
        for edge, colors in [(512, 128), (384, 128), (256, 64), (192, 64), (128, 32)]:
            resized = []
            for frame in frames:
                current = frame.copy()
                current.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                if count > 1:
                    palette = current.convert('RGB').quantize(colors=colors - 1)
                    table = palette.getpalette() or []
                    palette.putpalette((table + [0] * 768)[:768])
                    palette.paste(255, mask=current.getchannel('A').point(lambda a: 255 if a < 128 else 0))
                    palette.info['transparency'] = 255
                    current = palette
                resized.append(current)
            output = io.BytesIO()
            if count > 1:
                options = {'loop': 0} if loop == 0 else {'loop': loop - 1} if loop > 1 else {}
                resized[0].save(output, format='GIF', save_all=True, append_images=resized[1:],
                    duration=durations, disposal=2, transparency=255, background=255, optimize=False, **options)
            else:
                resized[0].save(output, format='PNG', optimize=True)
            body = output.getvalue()
            if len(body) <= 512 * 1024:
                return {'image': base64.b64encode(body).decode('ascii'), 'mime': 'image/gif' if count > 1 else 'image/png',
                        'animated': count > 1, 'frames': count, 'width': resized[0].width, 'height': resized[0].height}
        raise ValueError('转换后仍超过 512 KB，请缩短动画或缩小图片后重试；不会转成静态图')
    except (UnidentifiedImageError, OSError, EOFError, Image.DecompressionBombError) as exc:
        raise ValueError('WebP 图片无法解码，请检查文件是否完整后重试') from exc
if __name__=='__main__':
 try:print(json.dumps(convert_sticker_webp(sys.stdin.buffer.read(819201))))
 except ValueError as exc:print(json.dumps({'error':str(exc)}))
