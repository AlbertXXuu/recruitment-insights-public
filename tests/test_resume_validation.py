"""Pure upload values and real, bounded document parsing using synthetic bytes."""
import base64
import hashlib
import io
import stat
import unittest
import warnings
import zipfile

from pypdf import PdfWriter

from recruitment_insights.domain.resumes import MAX_RESUME_BYTES, prepare_resume
from recruitment_insights.infrastructure.resume_documents import validate_document
from tests.resume_fixtures import make_docx, make_pdf


def encoded(content):
    return base64.b64encode(content).decode('ascii')


def rewrite_docx(changes=None, additions=()):
    changes = changes or {}
    source, target = io.BytesIO(make_docx()), io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as package:
        for name in original.namelist():
            if changes.get(name, True) is not None:
                package.writestr(name, changes.get(name, original.read(name)))
        for name, content in additions:
            package.writestr(name, content)
    return target.getvalue()


class ResumeValueTests(unittest.TestCase):
    def test_valid_metadata_preserves_unicode_name_and_real_hash(self):
        for filename, content, media in (
                ('简历.PDF', make_pdf(), 'application/pdf'),
                ('简历.DOCX', make_docx(), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')):
            with self.subTest(filename=filename):
                actual = prepare_resume(filename, encoded(content))
                self.assertEqual(actual, {'filename': filename, 'media_type': media, 'content': content,
                                          'size_bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()})

    def test_empty_and_oversized_files_are_rejected_at_exact_boundary(self):
        self.assertEqual(prepare_resume('cv.pdf', encoded(make_pdf(MAX_RESUME_BYTES)))['size_bytes'], MAX_RESUME_BYTES)
        for content in (b'', make_pdf(MAX_RESUME_BYTES + 1)):
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                prepare_resume('cv.pdf', encoded(content))

    def test_bad_or_oversized_base64_is_rejected_before_decoding(self):
        for value in (None, 1, b'YWJj', '€', '%%%%', 'YQ=', 'YQ===', 'YQ==\n', 'YR==', 'AAAA====',
                      'A' * (4 * ((MAX_RESUME_BYTES + 2) // 3) + 4)):
            with self.subTest(value=repr(value)[:25]), self.assertRaises(ValueError):
                prepare_resume('cv.pdf', value)

    def test_unsafe_filenames_are_rejected(self):
        for filename in (None, 3, '', 'x' * 121, '../cv.pdf', '..\\cv.pdf', 'C:cv.pdf',
                         'a<.pdf', 'a>.pdf', 'a".pdf', 'a|.pdf', 'a?.pdf', 'a*.pdf',
                         'a\n.pdf', 'a\x00.pdf', 'a\x7f.pdf', 'a\u202e.pdf', 'a\ud800.pdf',
                         'CON.pdf', 'nul.PDF', 'com1.pdf', 'LPT³.pdf', 'cv.pdf.', 'cv.txt'):
            with self.subTest(filename=repr(filename)), self.assertRaises(ValueError):
                prepare_resume(filename, encoded(make_pdf()))

    def test_normalization_trims_display_whitespace_and_combining_unicode(self):
        self.assertEqual(prepare_resume('  re\u0301sume\u0301.pdf  ', encoded(make_pdf()))['filename'], 'résumé.pdf')


class ResumeDocumentTests(unittest.TestCase):
    def test_actual_pdf_and_docx_are_accepted(self):
        for filename, content in (('cv.PDF', make_pdf()), ('简历.docx', make_docx()),
                                  ('boundary.pdf', make_pdf(MAX_RESUME_BYTES))):
            with self.subTest(filename=filename):
                self.assertIsNone(validate_document(filename, content))

    def test_disguised_and_truncated_files_are_rejected(self):
        for filename, content in (('cv.pdf', make_docx()), ('cv.docx', make_pdf()),
                                  ('cv.pdf', b'%PDF-1.7\nnot-a-document\n%%EOF'),
                                  ('cv.pdf', make_pdf()[:-20]), ('cv.docx', make_docx()[:-20]),
                                  ('cv.docx', b'PK\x03\x04corrupt'), ('cv.txt', make_pdf()),
                                  ('cv.pdf', b''), ('cv.pdf', make_pdf(MAX_RESUME_BYTES + 1))):
            with self.subTest(filename=filename, length=len(content)), self.assertRaises(ValueError):
                validate_document(filename, content)

    def test_empty_page_tree_and_encrypted_pdf_are_rejected(self):
        writer = PdfWriter()
        empty = io.BytesIO()
        writer.write(empty)
        with self.assertRaisesRegex(ValueError, '至少'):
            validate_document('cv.pdf', empty.getvalue())
        writer.add_blank_page(width=595, height=842)
        writer.encrypt('Synthetic-only-test-password')
        protected = io.BytesIO()
        writer.write(protected)
        with self.assertRaisesRegex(ValueError, '加密'):
            validate_document('cv.pdf', protected.getvalue())

    def test_non_docx_zip_and_missing_required_parts_are_rejected(self):
        for name in ('[Content_Types].xml', '_rels/.rels', 'word/document.xml'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_document('cv.docx', rewrite_docx({name: None}))

    def test_docx_main_types_roots_and_relationships_are_checked(self):
        source = zipfile.ZipFile(io.BytesIO(make_docx()))
        cases = [
            {'[Content_Types].xml': source.read('[Content_Types].xml').replace(b'wordprocessingml.document.main+xml', b'wordprocessingml.template.main+xml')},
            {'[Content_Types].xml': source.read('[Content_Types].xml').replace(b'wordprocessingml.document.main+xml', b'wordprocessingml.document.macroEnabled.main+xml')},
            {'word/document.xml': b'<document><body/></document>'},
            {'word/document.xml': b'<broken'},
            {'word/document.xml': b'<?xml version="1.0" encoding="NOTREAL"?><x/>'},
            {'_rels/.rels': source.read('_rels/.rels').replace(b'word/document.xml', b'other.xml')},
            {'_rels/.rels': source.read('_rels/.rels').replace(b'word/document.xml', b'///word/document.xml')},
            {'_rels/.rels': source.read('_rels/.rels').replace(b'Target="', b'TargetMode="External" Target="')},
        ]
        for changes in cases:
            with self.subTest(part=list(changes)), self.assertRaises(ValueError):
                validate_document('cv.docx', rewrite_docx(changes))

    def test_docx_dtd_and_entities_are_rejected_in_any_xml_part(self):
        for declaration in (b'<!DOCTYPE x [<!ENTITY y "hello">]><x>&y;</x>',
                            '<!DOCTYPE x><x/>'.encode('utf-16')):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, 'DTD'):
                validate_document('cv.docx', rewrite_docx(additions=[('word/extra.xml', declaration)]))

    def test_unsafe_zip_paths_duplicate_case_and_macro_members_are_rejected(self):
        for name in ('../outside.xml', '/outside.xml', 'word/../../outside.xml',
                     'word\\outside.xml', 'C:outside.xml', '%2e%2e/outside.xml',
                     'word/document.xml', 'WORD/document.xml', 'word/vbaProject.bin', 'word/run.exe'):
            with self.subTest(name=name), warnings.catch_warnings():
                warnings.simplefilter('ignore', UserWarning)
                with self.assertRaises(ValueError):
                    validate_document('cv.docx', rewrite_docx(additions=[(name, b'fixture')]))

    def test_expansion_and_member_count_limits_reject_zip_bombs(self):
        for additions in ([('word/large.bin', b'0' * (8 * 1024 * 1024 + 1))],
                          [('word/a.bin', b'0' * (6 * 1024 * 1024)),
                           ('word/b.bin', b'0' * (6 * 1024 * 1024)),
                           ('word/c.bin', b'0' * (6 * 1024 * 1024))],
                          [(f'word/part{i}.xml', b'<x/>') for i in range(256)]):
            with self.subTest(members=len(additions)), self.assertRaises(ValueError):
                validate_document('cv.docx', rewrite_docx(additions=additions))

    def test_symlink_member_is_rejected(self):
        symlink = zipfile.ZipInfo('word/link')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, '符号链接'):
            validate_document('cv.docx', rewrite_docx(additions=[(symlink, b'../../outside')]))

    def test_encrypted_zip_flag_and_crc_corruption_are_rejected(self):
        protected = bytearray(make_docx())
        # Both local and central flags describe encryption; no actual key needed
        # to exercise the explicit unsupported-encryption contract.
        protected[6] |= 1
        central = protected.find(b'PK\x01\x02')
        protected[central + 8] |= 1
        with self.assertRaisesRegex(ValueError, '加密'):
            validate_document('cv.docx', bytes(protected))
        damaged = bytearray(make_docx())
        start = damaged.find(b'PK\x03\x04')
        name_length = int.from_bytes(damaged[start + 26:start + 28], 'little')
        extra_length = int.from_bytes(damaged[start + 28:start + 30], 'little')
        damaged[start + 30 + name_length + extra_length + 10] ^= 0x55
        with self.assertRaises(ValueError):
            validate_document('cv.docx', bytes(damaged))


if __name__ == '__main__':
    unittest.main()
