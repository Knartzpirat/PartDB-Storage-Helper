"""Netzlaufwerk-Buchstaben im Vorlagen-Ordner werden zum UNC-Pfad."""
from labeltool import desktop

REMOTE = {"Z": r"\\UnraidHome\Dokumente", "Y": "\\\\UnraidHome\\Media\\"}
ZIEL = r"\\UnraidHome\Dokumente\Projekte\Etiketten"


def fake(letter):
    return REMOTE.get(letter)


def test_unc_path():
    u = lambda p: desktop.unc_path(p, remote=fake)
    assert u(r"Z:\Projekte\Etiketten") == ZIEL
    assert u("z:/a/b/") == r"\\UnraidHome\Dokumente\a\b"
    assert u("Y:") == r"\\UnraidHome\Media"
    assert u(r"C:\Users\x") == r"C:\Users\x"                     # lokales Laufwerk bleibt
    assert u(r"\\Server\Freigabe\Ordner") == r"\\Server\Freigabe\Ordner"
    assert u("") == "" and u("relativ/ordner") == "relativ/ordner"


def test_helfer_und_assistent_nutzen_unc(tmp_path, monkeypatch):
    from labeltool import helfer, helfer_setup
    monkeypatch.setattr(desktop, "_drive_remote", fake)
    monkeypatch.setattr(desktop.unc_path, "__defaults__", (fake,))
    cfg = tmp_path / "helfer.yaml"
    cfg.write_text('server: "http://s:1"\ntoken: "t"\n' + r"vorlagen_ordner: 'Z:\Projekte\Etiketten'" + "\n",
                   encoding="utf-8")
    # auch unter Linux (CI): Windows-Pfade gelten als absolut und werden nicht an den Konfig-Ordner gehängt
    assert str(helfer.load_helper_config(cfg).vorlagen_ordner) == ZIEL

    w = helfer_setup.Werte(server="http://s:1", token="abc", vorlagen_ordner=r"Z:\Projekte\Etiketten",
                           drucker_modell="PT-P710BT", drucker="", autostart=False)
    helfer_setup.schreibe_helfer_yaml(tmp_path / "neu.yaml", w)
    import yaml
    assert yaml.safe_load((tmp_path / "neu.yaml").read_text(encoding="utf-8"))["vorlagen_ordner"] == ZIEL
