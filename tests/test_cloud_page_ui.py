from pathlib import Path


INDEX_HTML = (Path(__file__).resolve().parent.parent / "index.html").read_text(encoding="utf-8")


def test_cloud_navigation_and_page_are_compact():
    assert ">Bulut</button>" in INDEX_HTML
    assert ">Tarjima va bulut</button>" not in INDEX_HTML
    assert "Tarjima uchun video yuklang yoki bulutga tushgan" not in INDEX_HTML
    assert 'id="cloudInboxList" class="vgrid"' in INDEX_HTML
    assert 'id="cloudFab"' in INDEX_HTML
    assert 'aria-label="Bulutga video yoki zip yuklash"' in INDEX_HTML


def test_cloud_page_uses_library_style_cards_and_refreshes_directly():
    assert 'class="vcard status-upload cloud-vcard"' in INDEX_HTML
    assert "Kutubxonaga qo'shish" in INDEX_HTML
    assert "if (pageId === 'page-translate') loadCloudInbox();" in INDEX_HTML
    assert "else if (id === 'page-translate') loadCloudInbox();" in INDEX_HTML
    assert "switchTarjimaSubpage" not in INDEX_HTML
