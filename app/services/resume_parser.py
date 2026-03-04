"""
Extract raw text from uploaded resume files (PDF / DOCX).
"""

import io
from fastapi import UploadFile, HTTPException


async def extract_resume_text(file: UploadFile) -> str:
    content = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        return _extract_pdf(content)
    elif filename.endswith((".docx", ".doc")):
        return _extract_docx(content)
    else:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Upload a PDF or DOCX.",
        )


def _extract_pdf(content: bytes) -> str:
    try:
        import PyPDF2

        reader = PyPDF2.PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages)
        if not text.strip():
            raise HTTPException(
                status_code=422,
                detail="Could not extract text from PDF. Ensure it is text-based, not scanned.",
            )
        return text
    except ImportError:
        raise HTTPException(status_code=500, detail="PDF parsing library not available.")


def _extract_docx(content: bytes) -> str:
    try:
        from docx import Document

        doc = Document(io.BytesIO(content))
        return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
    except ImportError:
        raise HTTPException(status_code=500, detail="DOCX parsing library not available.")
