import sys


poda_templates = sys.modules[
    "src.tools.multi_step_service.workflows.poda_de_arvore.templates"
]


def test_solicitar_endereco_contains_guidance():
    message = poda_templates.solicitar_endereco()

    assert "Informe o endereço" in message
    assert "Exemplo" in message


def test_endereco_nao_localizado_includes_attempt_numbers():
    message = poda_templates.endereco_nao_localizado(2, 3)

    assert "tentativa 2/3" in message
    assert "não foi localizado" in message


def test_confirmar_endereco_embeds_formatted_address():
    formatted = "- Logradouro: Rua Teste\n- Cidade: Rio de Janeiro, RJ"
    message = poda_templates.confirmar_endereco(formatted)

    assert formatted in message
    assert "endereço está correto" in message.lower()


def test_confirmar_dados_salvos_lists_items():
    message = poda_templates.confirmar_dados_salvos(
        ["- CPF: ***78909", "- E-mail: u***@mail.com"]
    )

    assert "Você tem os seguintes dados salvos" in message
    assert "- CPF: ***78909" in message
    assert "- E-mail: u***@mail.com" in message


def test_solicitacao_criada_sucesso_contains_protocol():
    message = poda_templates.solicitacao_criada_sucesso("123456")

    assert "123456" in message
    assert "protocolo" in message.lower()
