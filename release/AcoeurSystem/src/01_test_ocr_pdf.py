from pathlib import Path
import fitz  # PyMuPDF
from paddleocr import PaddleOCR


PDF_PATH = Path("data/hospital_order.pdf")
OUT_DIR = Path("outputs/ocr_test")


def pdf_first_page_to_image(pdf_path: Path, out_dir: Path, dpi: int = 220) -> Path:
    """
    Convert the first page of the PDF to a PNG image.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    page = doc[0]

    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)

    pix = page.get_pixmap(matrix=mat, alpha=False)
    image_path = out_dir / "page_1.png"
    pix.save(image_path)

    return image_path


def main():
    if not PDF_PATH.exists():
        raise FileNotFoundError(f"PDF not found: {PDF_PATH}")

    image_path = pdf_first_page_to_image(PDF_PATH, OUT_DIR)
    print(f"First page image saved to: {image_path}")

    ocr = PaddleOCR(
        lang="fr",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        engine="paddle",
    )

    result = ocr.predict(str(image_path))

    for res in result:
        print("\n===== OCR RESULT =====")
        res.print()

        # Save OCR visualization and JSON result
        res.save_to_img(save_path=str(OUT_DIR))
        res.save_to_json(save_path=str(OUT_DIR))

    print(f"\nOCR output saved in: {OUT_DIR}")


if __name__ == "__main__":
    main()