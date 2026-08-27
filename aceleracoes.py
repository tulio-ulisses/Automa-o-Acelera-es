import os
import re
import csv
import io
import json
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from supabase import create_client


ARQUIVO_CONFIGURACAO = Path("configuracao.json")

TABELA_CONTENCIOSO = "contencioso"
TABELA_AGENDA = "agenda"
TABELA_ULTIMO_HISTORICO = "ultimo_historico_por_processo"
TABELA_CONTROLE = "aceleracoes_automacao"

URL_INDISPONIBILIDADES = (
    "https://docs.google.com/spreadsheets/d/"
    "14jTEqoL78n0AW218-IkSTuEjVJBUR8ceFIdUoNfMXpc/"
    "export?format=csv&gid=1844844874"
)

LOTE_SUPABASE = 1000
FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")

MAPA_NOMES_PLANILHA = {
    "henrique": "Henrique Lima",
    "joao": "João Ronaldo",
    "lais": "Lais Natalia",
    "matheus": "Matheus Padilha",
    "tainara": "Tainara Halmenschlager",
}


def env(nome):
    valor = os.getenv(nome)

    if not valor:
        raise RuntimeError(
            f"Variável de ambiente ausente: {nome}"
        )

    return valor


def normalizar_texto(valor):
    if valor is None:
        return ""

    texto = str(valor).strip()

    texto = unicodedata.normalize(
        "NFKD",
        texto,
    ).encode(
        "ASCII",
        "ignore",
    ).decode()

    texto = texto.casefold()

    texto = re.sub(
        r"[^a-z0-9]+",
        " ",
        texto,
    )

    return re.sub(
        r"\s+",
        " ",
        texto,
    ).strip()


def normalizar_processo(valor):
    if valor is None:
        return ""

    return re.sub(
        r"\D",
        "",
        str(valor),
    )


def valor_vazio(valor):
    if valor is None:
        return True

    return (
        str(valor).strip().lower()
        in {
            "",
            "none",
            "null",
            "nan",
            "nat",
        }
    )


def inteiro(valor):
    if valor_vazio(valor):
        return None

    try:
        return int(
            float(
                str(valor)
                .strip()
                .replace(",", ".")
            )
        )
    except (TypeError, ValueError):
        return None


def converter_data(valor):
    if valor_vazio(valor):
        return None

    texto = str(valor).strip()

    formatos = (
        ("%Y-%m-%d", texto[:10]),
        ("%d/%m/%Y", texto[:10]),
        ("%d-%m-%Y", texto[:10]),
        ("%d/%m/%y", texto[:8]),
    )

    for formato, trecho in formatos:
        try:
            return datetime.strptime(
                trecho,
                formato,
            ).date()
        except ValueError:
            continue

    return None


def carregar_configuracao():
    if not ARQUIVO_CONFIGURACAO.exists():
        raise RuntimeError(
            "Arquivo configuracao.json não encontrado."
        )

    with open(
        ARQUIVO_CONFIGURACAO,
        "r",
        encoding="utf-8",
    ) as arquivo:
        return json.load(arquivo)


def criar_supabase():
    return create_client(
        env("SUPABASE_URL"),
        env("SUPABASE_SERVICE_ROLE_KEY"),
    )


def ler_tabela(
    supabase,
    tabela,
):
    registros = []
    inicio = 0

    while True:
        fim = (
            inicio
            + LOTE_SUPABASE
            - 1
        )

        resposta = (
            supabase
            .table(tabela)
            .select("*")
            .range(
                inicio,
                fim,
            )
            .execute()
        )

        lote = resposta.data or []

        registros.extend(lote)

        if len(lote) < LOTE_SUPABASE:
            break

        inicio += LOTE_SUPABASE

    return registros


def resolver_coluna(
    registros,
    candidatos,
    obrigatoria=True,
):
    if not registros:
        if obrigatoria:
            raise RuntimeError(
                "Tabela sem registros."
            )

        return None

    colunas = list(
        registros[0].keys()
    )

    mapa = {
        normalizar_texto(coluna)
        .replace(" ", "_"): coluna
        for coluna in colunas
    }

    for candidato in candidatos:
        chave = (
            normalizar_texto(candidato)
            .replace(" ", "_")
        )

        if chave in mapa:
            return mapa[chave]

    if obrigatoria:
        raise RuntimeError(
            "Nenhuma das colunas esperadas "
            f"foi encontrada: {candidatos}. "
            f"Colunas disponíveis: {colunas}"
        )

    return None


def carregar_indisponibilidades(
    hoje,
):
    resposta = requests.get(
        URL_INDISPONIBILIDADES,
        timeout=30,
    )

    resposta.raise_for_status()

    leitor = csv.DictReader(
        io.StringIO(
            resposta.text
        )
    )

    indisponiveis = set()

    for linha in leitor:
        mapa_linha = {
            normalizar_texto(chave): valor
            for chave, valor in linha.items()
            if chave
        }

        nome = normalizar_texto(
            mapa_linha.get("nome")
        )

        data = converter_data(
            mapa_linha.get("data")
        )

        if not nome or not data:
            continue

        nome_completo = (
            MAPA_NOMES_PLANILHA.get(
                nome
            )
        )

        if not nome_completo:
            continue

        if data == hoje.date():
            indisponiveis.add(
                nome_completo
            )

    return indisponiveis


def data_execucao_valida(
    configuracao,
    hoje,
):
    if hoje.weekday() >= 5:
        return (
            False,
            "sábado ou domingo",
        )

    data_texto = hoje.strftime(
        "%Y-%m-%d"
    )

    dias_sem_execucao = set(
        configuracao.get(
            "dias_sem_execucao",
            [],
        )
    )

    if data_texto in dias_sem_execucao:
        return (
            False,
            "data configurada sem execução",
        )

    return True, ""


def responsavel_ausente_configuracao(
    configuracao,
    nome,
    hoje,
):
    data_texto = hoje.strftime(
        "%Y-%m-%d"
    )

    ausencias = (
        configuracao
        .get(
            "ausencias",
            {},
        )
        .get(
            nome,
            [],
        )
    )

    return (
        data_texto
        in ausencias
    )


def eh_risco_4(valor):
    texto = normalizar_texto(
        valor
    )

    return texto in {
        "4",
        "risco 4",
    }


def status_ativo(valor):
    return (
        normalizar_texto(valor)
        == "ativo"
    )


def responsavel_corresponde(
    registro,
    responsavel_config,
    coluna_id_responsavel,
    coluna_nome_responsavel,
):
    id_config = inteiro(
        responsavel_config.get(
            "idusuario"
        )
    )

    if coluna_id_responsavel:
        id_registro = inteiro(
            registro.get(
                coluna_id_responsavel
            )
        )

        if (
            id_registro is not None
            and id_config is not None
        ):
            return (
                id_registro
                == id_config
            )

    if not coluna_nome_responsavel:
        return False

    nome_registro = normalizar_texto(
        registro.get(
            coluna_nome_responsavel
        )
    )

    nome_config = normalizar_texto(
        responsavel_config.get(
            "nome"
        )
    )

    if nome_registro == nome_config:
        return True

    if (
        nome_registro
        and nome_config
        and nome_registro.startswith(
            nome_config
        )
    ):
        return True

    primeiro_registro = (
        nome_registro.split()[0]
        if nome_registro
        else ""
    )

    primeiro_config = (
        nome_config.split()[0]
        if nome_config
        else ""
    )

    return (
        primeiro_registro
        and primeiro_registro
        == primeiro_config
    )


def criar_mapa_ultimo_historico(
    historico,
):
    mapa = {}

    for registro in historico:
        idprocesso = str(
            registro.get(
                "idprocesso",
                ""
            )
        ).strip()

        data = registro.get(
            "data_ultimo_historico"
        )

        if (
            not idprocesso
            or valor_vazio(data)
        ):
            continue

        try:
            data_convertida = (
                datetime.fromisoformat(
                    str(data)[:10]
                )
            )
        except ValueError:
            continue

        mapa[idprocesso] = (
            data_convertida
        )

    return mapa


def obter_processos_acelerados_agenda(
    agenda,
    hoje,
):
    if not agenda:
        return set()

    coluna_processo = resolver_coluna(
        agenda,
        [
            "no_processo",
            "processo",
            "numero_processo",
            "n_de_processo",
            "n_do_processo",
            "texto10",
            "n_cumprimento_de_sentenca",
        ],
    )

    coluna_tipo = resolver_coluna(
        agenda,
        [
            "tipo_de_compromisso",
            "tipo_compromisso",
            "tipo",
            "compromisso",
            "idtipoagenda1",
            "idtipoagenda",
        ],
    )

    coluna_data = resolver_coluna(
        agenda,
        [
            "prazo",
            "data",
            "data1",
        ],
    )

    acelerados = set()

    for registro in agenda:
        tipo = registro.get(
            coluna_tipo
        )

        tipo_texto = normalizar_texto(
            tipo
        )

        tipo_id = inteiro(
            tipo
        )

        if not (
            tipo_id == 3
            or "aceler" in tipo_texto
        ):
            continue

        data_agenda = converter_data(
            registro.get(
                coluna_data
            )
        )

        if data_agenda != hoje.date():
            continue

        processo = normalizar_processo(
            registro.get(
                coluna_processo
            )
        )

        if processo:
            acelerados.add(
                processo
            )

    return acelerados


def obter_processos_controle(
    controle,
    hoje,
):
    bloqueados = set()

    for registro in controle:
        data_agendamento = converter_data(
            registro.get(
                "data_agendamento"
            )
        )

        if data_agendamento != hoje.date():
            continue

        status = normalizar_texto(
            registro.get(
                "status"
            )
        )

        if status in {
            "erro iilex",
            "cancelado",
        }:
            continue

        processo = normalizar_processo(
            registro.get(
                "processo"
            )
        )

        if processo:
            bloqueados.add(
                processo
            )

    return bloqueados


def selecionar_processos(
    contencioso,
    historico,
    agenda,
    controle,
    configuracao,
    indisponiveis,
    hoje,
):
    coluna_processo = resolver_coluna(
        contencioso,
        [
            "processo",
            "numero_processo",
            "n_de_processo",
            "n_do_processo",
            "texto10",
            "n_cumprimento_de_sentenca",
        ],
    )

    coluna_idprocesso = resolver_coluna(
        contencioso,
        [
            "idregistro",
            "id_processo",
            "idprocesso",
        ],
    )

    coluna_status = resolver_coluna(
        contencioso,
        [
            "status",
            "situacao",
        ],
    )

    coluna_risco = resolver_coluna(
        contencioso,
        [
            "risco",
            "risco_atual",
            "classificacao_risco",
            "classificacao_de_risco",
        ],
    )

    coluna_id_responsavel = resolver_coluna(
        contencioso,
        [
            "idusuarioresponsavel",
            "idusuario_responsavel",
            "idresponsavel",
            "id_responsavel",
            "idusuario1",
        ],
        obrigatoria=False,
    )

    coluna_nome_responsavel = resolver_coluna(
        contencioso,
        [
            "responsavel",
            "responsavel_da_pasta",
            "usuario_responsavel",
            "advogado_responsavel",
            "responsavel_atual",
        ],
        obrigatoria=(
            coluna_id_responsavel
            is None
        ),
    )

    mapa_historico = (
        criar_mapa_ultimo_historico(
            historico
        )
    )

    acelerados_agenda = (
        obter_processos_acelerados_agenda(
            agenda,
            hoje,
        )
    )

    acelerados_controle = (
        obter_processos_controle(
            controle,
            hoje,
        )
    )

    bloqueados = (
        acelerados_agenda
        | acelerados_controle
    )

    quantidade = int(
        configuracao.get(
            "quantidade_por_responsavel",
            2,
        )
    )

    resultado = {}

    for responsavel in configuracao.get(
        "responsaveis",
        [],
    ):
        nome = responsavel.get(
            "nome"
        )

        if not responsavel.get(
            "ativo",
            True,
        ):
            resultado[nome] = []
            continue

        if nome in indisponiveis:
            resultado[nome] = []
            continue

        if responsavel_ausente_configuracao(
            configuracao,
            nome,
            hoje,
        ):
            resultado[nome] = []
            continue

        candidatos = []

        for registro in contencioso:
            if not status_ativo(
                registro.get(
                    coluna_status
                )
            ):
                continue

            if eh_risco_4(
                registro.get(
                    coluna_risco
                )
            ):
                continue

            if not responsavel_corresponde(
                registro,
                responsavel,
                coluna_id_responsavel,
                coluna_nome_responsavel,
            ):
                continue

            processo_original = str(
                registro.get(
                    coluna_processo,
                    ""
                )
            ).strip()

            processo = normalizar_processo(
                processo_original
            )

            if not processo:
                continue

            if processo in bloqueados:
                continue

            idprocesso = str(
                registro.get(
                    coluna_idprocesso,
                    ""
                )
            ).strip()

            if not idprocesso:
                continue

            data_ultimo_historico = (
                mapa_historico.get(
                    idprocesso
                )
            )

            if not data_ultimo_historico:
                continue

            candidatos.append(
                {
                    "processo":
                        processo,

                    "processo_original":
                        processo_original,

                    "idprocesso":
                        idprocesso,

                    "responsavel":
                        nome,

                    "idusuario":
                        responsavel.get(
                            "idusuario"
                        ),

                    "risco":
                        registro.get(
                            coluna_risco
                        ),

                    "status":
                        registro.get(
                            coluna_status
                        ),

                    "data_ultimo_historico":
                        data_ultimo_historico,
                }
            )

        candidatos.sort(
            key=lambda item:
                item[
                    "data_ultimo_historico"
                ]
        )

        resultado[nome] = (
            candidatos[
                :quantidade
            ]
        )

    return resultado


def mostrar_resultado(
    selecionados,
    indisponiveis,
    hoje,
):
    print()
    print(
        "=" * 90
    )
    print(
        "SIMULAÇÃO DE ACELERAÇÕES"
    )
    print(
        "=" * 90
    )

    print(
        f"Data: "
        f"{hoje.strftime('%d/%m/%Y')}"
    )

    if indisponiveis:
        print(
            "Indisponíveis hoje: "
            + ", ".join(
                sorted(
                    indisponiveis
                )
            )
        )
    else:
        print(
            "Indisponíveis hoje: nenhum"
        )

    print()

    total = 0

    for responsavel, processos in (
        selecionados.items()
    ):
        if responsavel in indisponiveis:
            print(
                f"{responsavel}: "
                "INDISPONÍVEL"
            )
            print()
            continue

        print(
            f"{responsavel}: "
            f"{len(processos)} processo(s)"
        )

        for numero, item in enumerate(
            processos,
            start=1,
        ):
            dias_sem_movimentacao = (
                hoje.replace(
                    tzinfo=None
                )
                - item[
                    "data_ultimo_historico"
                ]
            ).days

            print(
                f"  {numero}. "
                f"{item['processo_original']} | "
                f"ID {item['idprocesso']} | "
                f"Status {item['status']} | "
                f"Risco {item['risco']} | "
                f"Último histórico: "
                f"{item['data_ultimo_historico'].strftime('%d/%m/%Y')} | "
                f"{dias_sem_movimentacao} dias"
            )

        print()

        total += len(
            processos
        )

    print(
        "-" * 90
    )

    print(
        f"TOTAL QUE SERIA AGENDADO: "
        f"{total}"
    )

    print(
        "-" * 90
    )

    print()
    print(
        "MODO SIMULAÇÃO: "
        "nenhum agendamento foi criado."
    )


def main():
    configuracao = (
        carregar_configuracao()
    )

    hoje = datetime.now(
        FUSO_BRASILIA
    )

    executar, motivo = (
        data_execucao_valida(
            configuracao,
            hoje,
        )
    )

    if not executar:
        print(
            "Execução ignorada: "
            f"{motivo}."
        )
        return

    print(
        "Lendo indisponibilidades..."
    )

    indisponiveis = (
        carregar_indisponibilidades(
            hoje
        )
    )

    print(
        "Indisponíveis hoje: "
        + (
            ", ".join(
                sorted(
                    indisponiveis
                )
            )
            if indisponiveis
            else "nenhum"
        )
    )

    supabase = criar_supabase()

    print(
        "Carregando Contencioso..."
    )

    contencioso = ler_tabela(
        supabase,
        TABELA_CONTENCIOSO,
    )

    print(
        f"Contencioso: "
        f"{len(contencioso):,}"
    )

    print(
        "Carregando último Histórico..."
    )

    historico = ler_tabela(
        supabase,
        TABELA_ULTIMO_HISTORICO,
    )

    print(
        f"Último Histórico: "
        f"{len(historico):,}"
    )

    print(
        "Carregando Agenda..."
    )

    agenda = ler_tabela(
        supabase,
        TABELA_AGENDA,
    )

    print(
        f"Agenda: "
        f"{len(agenda):,}"
    )

    print(
        "Carregando controle..."
    )

    controle = ler_tabela(
        supabase,
        TABELA_CONTROLE,
    )

    print(
        f"Controle: "
        f"{len(controle):,}"
    )

    selecionados = selecionar_processos(
        contencioso,
        historico,
        agenda,
        controle,
        configuracao,
        indisponiveis,
        hoje,
    )

    mostrar_resultado(
        selecionados,
        indisponiveis,
        hoje,
    )


if __name__ == "__main__":
    main()