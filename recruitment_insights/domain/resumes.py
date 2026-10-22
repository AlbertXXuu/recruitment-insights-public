"""Resume upload values without persistence, document parsers or filesystem access."""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import unicodedata


MAX_RESUME_BYTES = 2 * 1024 * 1024
ALLOWED_EXTENSIONS = ('.pdf', '.docx')
_MAX_ENCODED_LENGTH = 4 * ((MAX_RESUME_BYTES + 2) // 3)
_MEDIA_TYPES = {
    '.pdf': 'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
}
_RESERVED_NAME = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$', re.I)


def prepare_resume(filename, content_base64):
    """Validate transport values; actual PDF/OOXML parsing belongs to infrastructure."""
    if not isinstance(filename, str) or not filename or len(filename) > 120:
        raise ValueError('文件名须为 1～120 个字符。')
    if (any(unicodedata.category(char).startswith('C') for char in filename)
            or any(char in filename for char in '/\\<>:"|?*')):
        raise ValueError('文件名不能包含路径、控制字符或特殊字符。')
    filename = unicodedata.normalize('NFC', filename).strip()
    if not filename or len(filename) > 120 or filename.endswith('.'):
        raise ValueError('文件名无效，请重新命名后上传。')
    if _RESERVED_NAME.fullmatch(filename.split('.', 1)[0].rstrip(' ')):
        raise ValueError('文件名不能使用系统保留名称。')
    extension = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError('简历仅支持 PDF 或 DOCX 格式。')
    if not isinstance(content_base64, str):
        raise ValueError('文件内容编码无效，请重新选择文件。')
    if len(content_base64) > _MAX_ENCODED_LENGTH:
        raise ValueError('简历大小不能超过 2 MiB。')
    try:
        content = base64.b64decode(content_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError('文件内容编码无效，请重新选择文件。') from None
    # Re-encoding rejects extra padding and ambiguous non-canonical encodings too.
    if base64.b64encode(content).decode('ascii') != content_base64:
        raise ValueError('文件内容编码无效，请重新选择文件。')
    if not content:
        raise ValueError('简历文件不能为空。')
    if len(content) > MAX_RESUME_BYTES:
        raise ValueError('简历大小不能超过 2 MiB。')
    return {'filename': filename, 'media_type': _MEDIA_TYPES[extension],
            'content': content, 'size_bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest()}
