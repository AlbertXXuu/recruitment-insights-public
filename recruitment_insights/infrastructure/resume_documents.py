"""Bounded, in-memory structural checks; neither a renderer nor a virus scanner."""
from __future__ import annotations

import io
import posixpath
import re
import stat
import struct
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile
import zlib

from pypdf import PdfReader

from recruitment_insights.domain.resumes import MAX_RESUME_BYTES


_MAX_ZIP_MEMBERS = 256
_MAX_ZIP_EXPANDED_BYTES = 16 * 1024 * 1024
_MAX_ZIP_MEMBER_BYTES = 8 * 1024 * 1024
_CONTENT_TYPES_NS = 'http://schemas.openxmlformats.org/package/2006/content-types'
_RELATIONSHIPS_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_OFFICE_RELATIONSHIPS = {
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument',
    'http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument',
}
_WORD_NAMESPACES = {
    'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
    'http://purl.oclc.org/ooxml/wordprocessingml/main',
}
_MAIN_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'


class _InvalidDocument(ValueError):
    """Distinguish deliberate messages from parser implementation exceptions."""


def validate_document(filename: str, content: bytes) -> None:
    """Require a real readable PDF or a bounded ordinary DOCX package."""
    if not isinstance(content, bytes) or not content or len(content) > MAX_RESUME_BYTES:
        raise ValueError('简历文件不能为空，且不能超过 2 MiB。')
    extension = filename.rsplit('.', 1)[-1].lower()
    if extension == 'pdf':
        _validate_pdf(content)
    elif extension == 'docx':
        _validate_docx(content)
    else:
        raise ValueError('简历仅支持 PDF 或 DOCX 格式。')


def _validate_pdf(content):
    if not re.match(rb'%PDF-[12]\.[0-9](?:\r|\n|\s)', content) or not content.rstrip().endswith(b'%%EOF'):
        raise ValueError('PDF 文件结构无效，请上传完整的 PDF 文件。')
    try:
        reader = PdfReader(io.BytesIO(content), strict=True, root_object_recovery_limit=1000)
        if reader.is_encrypted:
            raise _InvalidDocument('不支持加密 PDF，请先移除密码保护。')
        count = len(reader.pages)
        if not count:
            raise _InvalidDocument('PDF 至少需要包含一页。')
        if count > 200:
            raise _InvalidDocument('简历 PDF 不能超过 200 页。')
        for page in reader.pages:
            # Resolve page objects and media boxes, but never extract or decompress
            # page streams, execute actions, or render untrusted document content.
            if page.get('/Type') != '/Page' or page.mediabox.width <= 0 or page.mediabox.height <= 0:
                raise _InvalidDocument('PDF 页面结构无效。')
    except _InvalidDocument:
        raise
    except Exception:
        # Parser exceptions include details about internal byte offsets or objects;
        # the upload contract deliberately returns only a stable user-facing error.
        raise ValueError('PDF 文件损坏或结构无效，请重新导出后上传。') from None


def _read_xml(data):
    # ElementTree does not load external DTDs; reject declarations explicitly as
    # a simpler guarantee, including UTF-16/32 encodings with interspersed NULs.
    declarations = data.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ValueError('DOCX XML 不能包含 DTD 或实体声明。')
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError, LookupError):
        raise ValueError('DOCX XML 内容损坏，请重新导出后上传。') from None


def _safe_member_name(name):
    decoded = unquote(name)
    parts = decoded.split('/')
    if (not name or len(name) > 240 or '\\' in decoded or ':' in decoded
            or decoded.startswith('/') or any(part in ('.', '..') for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
        raise ValueError('DOCX 包含不安全的文件路径。')
    return posixpath.normpath(decoded).casefold()


def _validate_docx(content):
    if not content.startswith(b'PK\x03\x04'):
        raise ValueError('DOCX 文件结构无效；不支持加密文档或其他格式改名。')
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as package:
            members = package.infolist()
            if not members or len(members) > _MAX_ZIP_MEMBERS:
                raise ValueError('DOCX 内部文件数量超过支持范围。')
            names, expanded, documents = set(), 0, {}
            for member in members:
                normalized = _safe_member_name(member.filename)
                if normalized in names:
                    raise ValueError('DOCX 包含重复的内部文件。')
                names.add(normalized)
                if member.flag_bits & 1:
                    raise ValueError('不支持加密 DOCX，请先移除密码保护。')
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError('DOCX 不能包含符号链接。')
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError('DOCX 使用了不支持的压缩方式。')
                if (member.file_size > _MAX_ZIP_MEMBER_BYTES
                        or expanded + member.file_size > _MAX_ZIP_EXPANDED_BYTES):
                    raise ValueError('DOCX 展开后过大，请压缩图片或重新导出。')
                if ('vbaproject' in normalized or 'vbadata' in normalized
                        or normalized.endswith(('.exe', '.dll', '.com', '.bat', '.cmd', '.js', '.vbs'))):
                    raise ValueError('不支持包含宏或可执行附件的 DOCX。')
                # Read bounded bytes from every member to validate CRC and avoid
                # trusting central-directory lengths or leaving corrupt media unchecked.
                with package.open(member) as stream:
                    data = stream.read(min(_MAX_ZIP_MEMBER_BYTES,
                                           _MAX_ZIP_EXPANDED_BYTES - expanded) + 1)
                if len(data) != member.file_size or len(data) > _MAX_ZIP_MEMBER_BYTES:
                    raise ValueError('DOCX 内部文件大小无效。')
                expanded += len(data)
                if member.filename.endswith(('.xml', '.rels')):
                    documents[member.filename] = _read_xml(data)
            _validate_docx_parts(documents)
    except ValueError:
        raise
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError,
            zlib.error, struct.error):
        raise ValueError('DOCX 文件损坏或压缩包结构无效，请重新导出后上传。') from None


def _validate_docx_parts(documents):
    required = ('[Content_Types].xml', '_rels/.rels', 'word/document.xml')
    if any(name not in documents for name in required):
        raise ValueError('文件不是有效的 DOCX 文档，缺少必要内容。')
    types, relationships, document = (documents[name] for name in required)
    if types.tag != '{' + _CONTENT_TYPES_NS + '}Types':
        raise ValueError('DOCX 内容类型清单无效。')
    main_types = []
    for item in types:
        content_type = item.get('ContentType', '')
        if 'macroenabled' in content_type.lower() or 'vba' in content_type.lower():
            raise ValueError('不支持包含宏的 DOCX。')
        if item.tag == '{' + _CONTENT_TYPES_NS + '}Override' and item.get('PartName') == '/word/document.xml':
            main_types.append(content_type)
    if main_types != [_MAIN_CONTENT_TYPE]:
        raise ValueError('DOCX 主文档内容类型无效，不支持模板或宏文档。')
    if relationships.tag != '{' + _RELATIONSHIPS_NS + '}Relationships':
        raise ValueError('DOCX 包关系清单无效。')
    main_relationships = [item for item in relationships
                          if item.tag == '{' + _RELATIONSHIPS_NS + '}Relationship'
                          and item.get('Type') in _OFFICE_RELATIONSHIPS]
    if (len(main_relationships) != 1
            or main_relationships[0].get('TargetMode', 'Internal') != 'Internal'
            or main_relationships[0].get('Target') not in ('word/document.xml', '/word/document.xml')):
        raise ValueError('DOCX 主文档关系无效。')
    if not any(document.tag == '{' + namespace + '}document'
               and document.find('{' + namespace + '}body') is not None
               for namespace in _WORD_NAMESPACES):
        raise ValueError('DOCX 主文档 XML 结构无效。')
