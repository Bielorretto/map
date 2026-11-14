"""Tests de l'assistant sans appel au LLM : normalisation, mémoire, consignes."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from api.assistant import OUTILS, SOURCES, SOURCES_OUTIL, SYSTEME, Assistant, norm, sans_article  # noqa: E402


def test_norm():
    assert norm("Saint-Étienne") == "saint etienne"
    assert norm("St Flour") == norm("Saint-Flour")
    assert norm("Côtes-d’Armor") == "cotes d armor"


def test_sans_article():
    assert sans_article(norm("le département de la Lozère")) == "lozere"
    assert sans_article(norm("région Occitanie")) == "occitanie"


def test_historique_alterne_et_finit_par_assistant():
    h = [{"role": "assistant", "texte": "bonjour"}, {"role": "user", "texte": "a"}, {"role": "user", "texte": "b"},
         {"role": "assistant", "texte": "c"}, {"role": "user", "texte": "sans réponse"}]
    m = Assistant.historique(h)
    assert [x["role"] for x in m] == ["user", "assistant"]
    assert m[0]["content"] == "a\n\nb"


def test_bascule_fallback_retire_les_blocs_anterieurs():
    c = [NS(type="thinking"), NS(type="tool_use"), NS(type="text"), NS(type="fallback"), NS(type="tool_use")]
    assert [b.type for b in Assistant.sans_partiel(c)] == ["text", "fallback", "tool_use"]


def test_chaque_outil_a_ses_sources():
    noms = {o["name"] for o in OUTILS}
    assert noms == set(SOURCES_OUTIL)
    assert all(i in SOURCES for ids in SOURCES_OUTIL.values() for i in ids)
    assert "{seuil}" in SYSTEME and "[FINESS]" in SYSTEME
