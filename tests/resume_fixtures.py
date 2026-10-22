"""Self-generated blank resume-format samples containing no personal information."""
import io
import zipfile

from pypdf import PdfWriter


def make_pdf(size_bytes=None):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    if size_bytes is not None:
        if size_bytes < len(content):
            raise ValueError('Requested fixture size is smaller than a minimal PDF.')
        # Keep the final startxref / offset / EOF lines together. Padding after
        # the xref table does not change any recorded object or xref offsets.
        padding = size_bytes - len(content)
        offset = content.rfind(b'startxref')
        if padding:
            content = content[:offset] + b' ' * (padding - 1) + b'\n' + content[offset:]
    return content


def make_docx():
    content_types = '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    relationships = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    document = '''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic resume validation fixture.</w:t></w:r></w:p></w:body></w:document>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, content in (('[Content_Types].xml', content_types),
                                  ('_rels/.rels', relationships),
                                  ('word/document.xml', document)):
            archive.writestr(filename, content.encode('utf-8'))
    return buffer.getvalue()
