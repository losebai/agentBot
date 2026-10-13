"""媒体内容的文件签名校验。"""


def matches_audio_signature(raw: bytes, media_format: str) -> bool:
    """按实际文件签名校验音频，避免仅信任声明的格式。"""
    if media_format == "mp3":
        return raw.startswith(b"ID3") or (
            len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0
        )
    if media_format == "opus":
        return raw.startswith(b"OggS")
    if media_format == "aac":
        return len(raw) >= 2 and raw[0] == 0xFF and raw[1] & 0xF6 == 0xF0
    if media_format == "flac":
        return raw.startswith(b"fLaC")
    if media_format == "wav":
        return len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WAVE"
    return len(raw) % 2 == 0
