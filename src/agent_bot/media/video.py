"""有界核对 MP4 容器完整结构与视频轨道，不承诺逐帧解码。"""

MAX_VIDEO_BYTES = 100 * 1024 * 1024


def validate_mp4(data: bytes) -> None:
    """拒绝截断 box、无视频轨道及伪装内容，输入上限独立于图片。"""
    if not 24 <= len(data) <= MAX_VIDEO_BYTES:
        raise ValueError("invalid MP4 size")

    def boxes(start: int, end: int):
        """按长度前进，不能在媒体内容中搜索伪造的 box 名称。"""
        while start < end:
            if end - start < 8:
                raise ValueError("truncated MP4 box")
            size = int.from_bytes(data[start : start + 4], "big")
            kind = data[start + 4 : start + 8]
            header = 8
            if size == 1:
                if end - start < 16:
                    raise ValueError("truncated extended MP4 box")
                size = int.from_bytes(data[start + 8 : start + 16], "big")
                header = 16
            elif size == 0:
                size = end - start
            if size < header or start + size > end:
                raise ValueError("invalid MP4 box size")
            yield kind, start + header, start + size
            start += size

    top = list(boxes(0, len(data)))
    if top[0][0] != b"ftyp" or top[0][2] - top[0][1] < 8:
        raise ValueError("MP4 file type is missing")
    if not any(kind == b"mdat" and end > start for kind, start, end in top):
        raise ValueError("MP4 media data is missing")
    video = False
    for kind, start, end in top:
        if kind != b"moov":
            continue
        for child, first, last in boxes(start, end):
            if child != b"trak":
                continue
            for item, a, b in boxes(first, last):
                if item == b"mdia":
                    for field, x, y in boxes(a, b):
                        if field == b"hdlr" and y - x >= 24 and data[x + 8 : x + 12] == b"vide":
                            video = True
    if not video:
        raise ValueError("MP4 video track is missing")
