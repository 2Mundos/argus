import pytest
from argus_core.accounts import fallback_tier
from argus_core.complexity import MAX_TRIVIAL_SPEC, Complexity, classify

T, S = Complexity.TRIVIAL, Complexity.STANDARD


@pytest.mark.parametrize(
    ("title", "spec"),
    [
        ("Fix typo in the pricing page", ""),
        ("Corrigir erro de digitação no rodapé", ""),
        ("Ajustar texto do botão de login", "Trocar 'Entrar' por 'Acessar'."),
        ("Change the copy on the landing hero", "Shorter wording, same meaning."),
        ("Atualizar a tradução para inglês", ""),
        ("Improve onboarding wording", "docs only, no behaviour change"),
        ("Revisar redação da página Sobre", ""),
        ("README: translate the intro", ""),
    ],
)
def test_clear_copy_changes_are_trivial(title: str, spec: str) -> None:
    assert classify(title, spec) is T


@pytest.mark.parametrize(
    ("title", "spec"),
    [
        ("Add a retry to the sync worker", ""),
        ("Refactor the account service", ""),
        ("Fix typo in the API endpoint name", ""),  # copy + risk word
        ("Corrigir texto da tela de autenticação", ""),
        ("Update copy", "Also add a migration for the new column"),
        ("Change the copy on the checkout", "and cover it with a test"),
        ("Rotate the credential text", ""),
        ("Fix the bug in the wording check", ""),
        ("Deploy the new copy", ""),
        ("", ""),
    ],
)
def test_anything_with_a_risk_signal_or_no_copy_signal_is_standard(
    title: str, spec: str
) -> None:
    assert classify(title, spec) is S


def test_a_long_spec_is_standard_even_for_a_typo() -> None:
    assert classify("Fix typo", "x" * MAX_TRIVIAL_SPEC) is T
    assert classify("Fix typo", "x" * (MAX_TRIVIAL_SPEC + 1)) is S


def test_substrings_do_not_count() -> None:
    assert classify("Handle contexts and pretext", "") is S  # "text" inside words
    assert classify("Add textures to the renderer", "") is S


def test_prefer_local_inverts_only_the_tier() -> None:
    assert fallback_tier("anthropic") < fallback_tier("local")
    assert fallback_tier("local", prefer_local=True) < fallback_tier(
        "anthropic", prefer_local=True
    )
