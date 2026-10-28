"""Verteilung der Bauteile auf die Label-Felder: zusammenfassen mit Trenner statt abschneiden."""
from labeltool.labels import PartEntry, make_content_label
from labeltool.model import Box
from test_smoke import make


def box(box_type, name="3D-1.0005"):
    return Box(id=59, name=name, barcode="S0059", abbr="3D", content="3D Drucker", color="Mittelgrau",
               color_hex="#8a8a8a", compartments="1", box_type=box_type)


def teil(pid, name, kategorie, footprint="", fach=None):
    return PartEntry(part_id=pid, name=name, footprint=footprint, category=kategorie, compartment=fach, tagged=False)


# Box 3D-1.0005 vom Foto: WinBin1, fünf Bauteile, in dieser Reihenfolge angelegt
FOTO = [teil(1, "WK 5x5x25mm", "Wellenkupplung"), teil(2, "WK 5x8x25mm", "Wellenkupplung"),
        teil(3, "GT2 Zahnriemen", "GT-Riemen"), teil(4, "GT2 Zahnrad 20TB5W6", "GT-Riemen"),
        teil(5, "GT2 Führungsrolle 20TB5W6", "GT-Riemen")]


def test_ein_feld_alle_bauteile_wie_bisheriges_label(tmp_path):
    _, _, cfg = make(tmp_path)
    label = make_content_label(cfg, box("WinBin1"), list(reversed(FOTO)))   # Reihenfolge aus Part-DB egal
    row = label.row(cfg)
    assert row["Section1"] == ("WK 5x5x25mm | WK 5x8x25mm | GT2 Zahnriemen | GT2 Zahnrad 20TB5W6 | "
                               "GT2 Führungsrolle 20TB5W6")
    assert row["Category"] == "Wellenkupplung | GT-Riemen"
    assert "Section2" not in row                     # weitere CSV-Spalten schreibt write_csv leer
    assert "zusammengefasst" in label.warnings[0] and "fehlt" not in label.warnings[0]


def test_ueberlauf_verteilt_sich_auf_freie_felder(tmp_path):
    _, _, cfg = make(tmp_path)
    row = make_content_label(cfg, box("WinBin2"), FOTO).row(cfg)
    assert row["Section1"] == "WK 5x5x25mm | WK 5x8x25mm | GT2 Zahnriemen"      # 5 Teile -> 3 + 2
    assert row["Section2"] == "GT2 Zahnrad 20TB5W6 | GT2 Führungsrolle 20TB5W6"


def test_fach_bleibt_fach_und_doppelte_werden_zusammengefasst(tmp_path):
    _, _, cfg = make(tmp_path)
    teile = [teil(1, "A", "K1", "SOT-23", fach=2), teil(2, "B", "K1", "SOT-23", fach=2),
             teil(3, "C", "K2", "0805"), teil(4, "D", "K2", "0603")]
    label = make_content_label(cfg, box("3U2x1_3"), teile)
    row = label.row(cfg)
    assert row["Section2"] == "A | B" and row["Footprint2"] == "SOT-23"     # Footprint ohne Doppelung
    assert row["Section1"] == "C" and row["Section3"] == "D"
    assert row["Category"] == "K2 | K1"                                      # Reihenfolge der Felder


def test_kein_freies_feld_mehr(tmp_path):
    _, _, cfg = make(tmp_path)
    teile = [teil(1, "A", "K", fach=1), teil(2, "B", "K", fach=2), teil(3, "C", "K")]
    row = make_content_label(cfg, box("WinBin2"), teile).row(cfg)
    assert {row["Section1"], row["Section2"]} == {"A | C", "B"}            # Rest ins Feld mit den wenigsten


def test_eigener_trenner_und_genug_felder(tmp_path):
    _, _, cfg = make(tmp_path)
    cfg.part_separator = " / "
    label = make_content_label(cfg, box("WinBin1"), FOTO[:2])
    assert label.row(cfg)["Section1"] == "WK 5x5x25mm / WK 5x8x25mm"
    label = make_content_label(cfg, box("WinBin3"), FOTO[:3])
    assert label.warnings == [] and label.row(cfg)["Section3"] == "GT2 Zahnriemen"


def test_fachnummer_erkennung(tmp_path):
    from labeltool.labels import compartment_of
    _, _, cfg = make(tmp_path)
    fach = lambda **lot: compartment_of(lot, cfg.compartment_regex)
    # nur die Zahl, "Fach 3", "F3" – auch mit Text dahinter
    assert [fach(description=d) for d in ("3", " 4 ", "Fach 3", "F2", "fach:5", "Fach 1 – oben")] == [3, 4, 3, 2, 5, 1]
    # Mengen/Text sind kein Fach
    assert [fach(description=d) for d in ("3 Rollen", "100 Stück", "5 V", "Links Fach 2", "")] == [None] * 5
    # Kommentar als Ersatz, Beschreibung hat Vorrang
    assert fach(description="Rest", comment="2") == 2 and fach(description="1", comment="2") == 1


def test_nur_zahl_ordnet_ins_feld(tmp_path):
    from labeltool.labels import compartment_of
    _, _, cfg = make(tmp_path)
    teile = [teil(i, n, "K", fach=compartment_of({"description": d}, cfg.compartment_regex))
             for i, (n, d) in enumerate([("A", "3"), ("B", "1"), ("C", "")], start=1)]
    row = make_content_label(cfg, box("WinBin3"), teile).row(cfg)
    assert (row["Section1"], row["Section2"], row["Section3"]) == ("B", "C", "A")
