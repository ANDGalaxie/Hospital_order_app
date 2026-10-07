"""Opt-in local parity evidence; never seed or update business documents.

Run with DJANGO_SETTINGS_MODULE=commercial_ui_preview_settings and the
already seeded synthetic /tmp preview SQLite database. Requires Playwright.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path


def main():
    if os.environ.get("DJANGO_SETTINGS_MODULE") != "commercial_ui_preview_settings":
        raise SystemExit("Use the isolated synthetic commercial_ui_preview_settings only.")
    import django
    django.setup()
    from django.apps import apps
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.test import Client
    from documents.models import GeneratedDocument
    from shipments.models import ShipmentBatch
    from workflow.models import DocumentWorkflowItem
    from commercial_pos.services.generation_service import render_commercial_files

    if str(settings.DATABASES["default"]["NAME"]) != "/tmp/commercial-po-preview.sqlite3":
        raise SystemExit("Unexpected database.")
    evidence = settings.BASE_DIR / "artifacts/commercial_ui_parity"
    screenshots = evidence / "screenshots"
    screenshots.mkdir(parents=True, exist_ok=True)
    batch = ShipmentBatch.objects.get(order__bon_de_commande="147891", batch_number=1)
    original = GeneratedDocument.objects.get(shipment_batch=batch, document_type="factory_po")
    commercial = GeneratedDocument.objects.get(shipment_batch=batch, document_type="commercial_po")

    def business_digest():
        states = {}
        for label in ("orders", "shipments", "factory_confirmations", "documents", "workflow", "settlements", "backorders"):
            for model in apps.get_app_config(label).get_models():
                rows = list(model.objects.order_by("pk").values())
                states[model._meta.label] = hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()
        return states

    before = business_digest()
    original_state = hashlib.sha256(json.dumps(GeneratedDocument.objects.filter(pk=original.pk).values().get(), sort_keys=True, default=str).encode()).hexdigest()
    original_hashes = {kind: hashlib.sha256(Path(getattr(original, kind+"_file").path).read_bytes()).hexdigest() for kind in ("pdf", "html")}
    pdf_directory = evidence / "pdf"
    pdf_directory.mkdir(exist_ok=True)
    shutil.copyfile(original.pdf_file.path, pdf_directory / "Factory_PO_147891_existing.pdf")
    render_directory = pdf_directory / "commercial_147891"
    render_directory.mkdir(exist_ok=True)
    render_commercial_files(commercial.source_data, render_directory)
    import fitz
    for name, path in (("factory_147891", pdf_directory / "Factory_PO_147891_existing.pdf"), ("commercial_147891", render_directory / "document.pdf")):
        with fitz.open(path) as pdf:
            pdf[0].get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(pdf_directory / (name + ".png"))

    sessions = {}
    for role, username in (("reference", "synthetic-commercial-internal"), ("commercial", "synthetic-commercial-viewer")):
        client = Client()
        client.force_login(get_user_model().objects.get(username=username))
        sessions[role] = client.cookies[settings.SESSION_COOKIE_NAME].value
    pairs = {
        "purchase_list": ("/portal/factory/", "/portal/commercial/purchase-orders/"),
        "purchase_detail": (f"/portal/factory/{batch.factory_confirmation_id}/", f"/portal/commercial/purchase-orders/batches/{batch.pk}/"),
        "operations_list": ("/portal/workflow/", "/portal/commercial/operations/"),
        "operations_detail": (f"/portal/workflow/{DocumentWorkflowItem.objects.get(shipment_batch=batch).pk}/", f"/portal/commercial/operations/batches/{batch.pk}/"),
        "finance": ("/portal/finance/", "/portal/commercial/finance/"),
    }
    checks, failures, metrics = [], [], []
    batch_documents = list(GeneratedDocument.objects.filter(shipment_batch=batch))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        for language in ("zh-hans", "en", "fr"):
            for viewport_name, viewport in (("desktop", {"width": 1440, "height": 1000}), ("mobile", {"width": 390, "height": 844})):
                for role in ("reference", "commercial"):
                    context = browser.new_context(viewport=viewport)
                    context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": sessions[role], "url": "http://127.0.0.1:8769"},
                                         {"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": "http://127.0.0.1:8769"}])
                    page = context.new_page()
                    page.on("response", lambda response: failures.append({"url": response.url, "status": response.status}) if response.status >= 400 else None)
                    for name, urls in pairs.items():
                        response = page.goto("http://127.0.0.1:8769" + urls[0 if role == "reference" else 1], wait_until="networkidle")
                        assert response.status == 200, (name, role, response.status)
                        page.screenshot(path=str(screenshots / f"{language}_{viewport_name}_{name}_{role}.png"), full_page=True)
                        measurements = page.evaluate("""() => ({
                          pageWidth: document.querySelector('main.page').getBoundingClientRect().width,
                          overflow: document.documentElement.scrollWidth > innerWidth,
                          panels: [...document.querySelectorAll('main .portal-panel, main .finance-panel')].map(e => {
                            const s=getComputedStyle(e); return {radius:s.borderRadius, shadow:s.boxShadow, padding:s.padding};
                          }),
                          businessPosts: [...document.querySelectorAll('main form')].filter(f => f.method.toLowerCase()==='post').length,
                          css: [...document.querySelectorAll('link[rel=stylesheet]')].map(e=>e.href)
                        })""")
                        metrics.append({"language": language, "viewport": viewport_name, "module": name, "role": role, **measurements})
                        if role == "commercial":
                            assert not measurements["overflow"], (language, viewport_name, name, "page overflow")
                            assert measurements["businessPosts"] == 0
                            assert "commercial.css" not in page.content()
                            assert "DELAHK" not in page.content()
                            assert "/admin/" not in page.content()
                    context.close()

        context = browser.new_context()
        context.add_cookies([{"name": settings.SESSION_COOKIE_NAME, "value": sessions["commercial"], "url": "http://127.0.0.1:8769"}])
        request = context.request
        for doc in batch_documents:
            for kind in ("pdf", "html"):
                for suffix in ("", "?download=1"):
                    url = f"/portal/commercial/documents/{doc.pk}/{kind}/{suffix}"
                    response = request.get("http://127.0.0.1:8769" + url, max_redirects=0)
                    expected = 404 if doc.document_type == "factory_po" else 200
                    assert response.status == expected
                    checks.append({"url": url, "status": response.status})
        for url in ("/portal/workflow/", "/portal/finance/", "/portal/settlements/", "/admin/", "/portal/files/"+original.pdf_file.name):
            response = request.get("http://127.0.0.1:8769"+url, max_redirects=0)
            assert response.status == 403
            checks.append({"url": url, "status": response.status})
        for prefix in ("/media/", "/outputs/"):
            response = request.get("http://127.0.0.1:8769"+prefix+original.pdf_file.name, max_redirects=0)
            assert response.status == 404
            checks.append({"url": prefix+original.pdf_file.name, "status": response.status})
        anonymous = browser.new_context()
        response = anonymous.request.get(f"http://127.0.0.1:8769/portal/commercial/documents/{commercial.pk}/pdf/", max_redirects=0)
        assert response.status == 302
        checks.append({"url": "anonymous commercial pdf", "status": response.status})
        anonymous.close()
        context.close()
        browser.close()
    assert not failures, failures
    assert before == business_digest(), "Business records changed during acceptance"
    assert original_state == hashlib.sha256(json.dumps(GeneratedDocument.objects.filter(pk=original.pk).values().get(), sort_keys=True, default=str).encode()).hexdigest()
    assert original_hashes == {kind: hashlib.sha256(Path(getattr(original, kind+"_file").path).read_bytes()).hexdigest() for kind in ("pdf", "html")}
    result = {"environment": "synthetic SQLite /tmp/commercial-po-preview.sqlite3; DEBUG=False; WhiteNoise; STATIC_ROOT=staticfiles",
              "template": "templates/factory_purchase_order.html", "price": commercial.source_data["unit_price"],
              "total": commercial.source_data["total_amount"], "quantity": commercial.source_data["total_units"],
              "original_factory_unchanged": True, "business_tables_unchanged": True,
              "original_factory_hashes": original_hashes, "screenshots": 60, "failed_resources": failures,
              "checks": checks, "metrics": metrics, "reverse_proxy_verified": False}
    (evidence / "visual_acceptance.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print("60 paired screenshots; PDF evidence retained; no failed resources; original files/business rows unchanged.")


if __name__ == "__main__":
    main()
