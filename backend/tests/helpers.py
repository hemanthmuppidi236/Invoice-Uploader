"""Small builders shared by the tests."""

import io


def make_pdf(pages: int = 1) -> bytes:
    """A real, minimal PDF with `pages` blank pages.

    A real one rather than b"%PDF fake": the splitter actually parses what it
    is given, and a fake would make every test about it vacuous.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# Deliberately no text-layer PDF builder here. Constructing one by hand needs
# a font resource dictionary to be extractable, and a helper that silently
# produces unreadable text would make every test using it vacuous. The two
# places that read a page's text — yard detection and the CUSTOMER JOB NO.
# hint — take a string, so they are tested directly, and the integration
# tests stub `pdf_split.page_text`.
