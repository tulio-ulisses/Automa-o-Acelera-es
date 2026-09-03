import os
import re
import csv
import io
import json
import time
import base64
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from supabase import create_client


ARQUIVO_CONFIGURACAO = Path("configuracao.json")

BASE_URL = "https://juscash.iilex.com.br/sistema"
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
INTERVALO_ENTRE_AGENDAMENTOS = 2.5
ESPERA_429 = 65
MAX_TENTATIVAS_POST = 5

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

    if valor:
        return valor

    try:
        import credenciais
    except ImportError:
        credenciais = None

    if credenciais:
        valor = getattr(
            credenciais,
            nome,
            None,
        )

        if valor:
            return valor

    raise RuntimeError(
        f"Credencial ausente: {nome}. "
        "Configure como variável de ambiente "
        "ou no arquivo credenciais.py."
    )


def normalizar_texto(valor):
    if valor is None:
        return ""

    texto = str(valor).strip()
    texto = unicodedata.normalize("NFKD", texto).encode("ASCII", "ignore").decode()
    texto = texto.casefold()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def normalizar_processo(valor):
    if valor is None:
        return ""
    return re.sub(r"\D", "", str(valor))


def valor_vazio(valor):
    if valor is None:
        return True
    return str(valor).strip().lower() in {"", "none", "null", "nan", "nat"}


def inteiro(valor):
    if valor_vazio(valor):
        return None

    try:
        return int(float(str(valor).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None


def converter_data(valor):
    if valor_vazio(valor):
        return None

    texto = str(valor).strip()

    tentativas = [
        ("%Y-%m-%d", texto[:10]),
        ("%d/%m/%Y", texto[:10]),
        ("%d-%m-%Y", texto[:10]),
        ("%d/%m/%y", texto[:8]),
    ]

    for formato, trecho in tentativas:
        try:
            return datetime.strptime(trecho, formato).date()
        except ValueError:
            continue

    return None


def carregar_configuracao():
    if not ARQUIVO_CONFIGURACAO.exists():
        raise RuntimeError("Arquivo configuracao.json não encontrado.")

    with open(ARQUIVO_CONFIGURACAO, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def criar_supabase():
    return create_client(
        env("SUPABASE_URL"),
        env("SUPABASE_SERVICE_ROLE_KEY"),
    )


def criar_sessao_iilex():
    username = env("IILEX_USERNAME")
    password = env("IILEX_PASSWORD")

    token = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("utf-8")

    sessao = requests.Session()
    sessao.headers.update(
        {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
        }
    )
    return sessao


def ler_tabela(supabase, tabela):
    registros = []
    inicio = 0

    while True:
        fim = inicio + LOTE_SUPABASE - 1

        resposta = (
            supabase
            .table(tabela)
            .select("*")
            .range(inicio, fim)
            .execute()
        )

        lote = resposta.data or []
        registros.extend(lote)

        if len(lote) < LOTE_SUPABASE:
            break

        inicio += LOTE_SUPABASE

    return registros


def resolver_coluna(registros, candidatos, obrigatoria=True):
    if not registros:
        if obrigatoria:
            raise RuntimeError("Tabela sem registros.")
        return None

    colunas = list(registros[0].keys())

    mapa = {
        normalizar_texto(coluna).replace(" ", "_"): coluna
        for coluna in colunas
    }

    for candidato in candidatos:
        chave = normalizar_texto(candidato).replace(" ", "_")
        if chave in mapa:
            return mapa[chave]

    if obrigatoria:
        raise RuntimeError(
            "Nenhuma das colunas esperadas foi encontrada: "
            f"{candidatos}. Colunas disponíveis: {colunas}"
        )

    return None


def carregar_indisponibilidades(hoje):
    resposta = requests.get(
        URL_INDISPONIBILIDADES,
        timeout=30,
    )
    resposta.raise_for_status()

    leitor = csv.DictReader(io.StringIO(resposta.text))
    indisponiveis = set()

    for linha in leitor:
        mapa_linha = {
            normalizar_texto(chave): valor
            for chave, valor in linha.items()
            if chave
        }

        nome = normalizar_texto(mapa_linha.get("nome"))
        data = converter_data(mapa_linha.get("data"))

        if not nome or not data:
            continue

        nome_completo = MAPA_NOMES_PLANILHA.get(nome)
        if not nome_completo:
            continue

        if data == hoje.date():
            indisponiveis.add(nome_completo)

    return indisponiveis


def data_execucao_valida(configuracao, hoje):
    if hoje.weekday() >= 5:
        return False, "sábado ou domingo"

    data_texto = hoje.strftime("%Y-%m-%d")
    dias_sem_execucao = set(configuracao.get("dias_sem_execucao", []))

    if data_texto in dias_sem_execucao:
        return False, "data configurada sem execução"

    return True, ""


def eh_risco_4(valor):
    return normalizar_texto(valor) in {"4", "risco 4"}


def status_ativo(valor):
    return normalizar_texto(valor) == "ativo"


def responsavel_corresponde(
    registro,
    responsavel_config,
    coluna_id_responsavel,
    coluna_nome_responsavel,
):
    id_config = inteiro(responsavel_config.get("idusuario"))

    if coluna_id_responsavel:
        id_registro = inteiro(registro.get(coluna_id_responsavel))

        if id_registro is not None and id_config is not None:
            return id_registro == id_config

    if not coluna_nome_responsavel:
        return False

    nome_registro = normalizar_texto(registro.get(coluna_nome_responsavel))
    nome_config = normalizar_texto(responsavel_config.get("nome"))

    if nome_registro == nome_config:
        return True

    if nome_registro and nome_config and nome_registro.startswith(nome_config):
        return True

    primeiro_registro = nome_registro.split()[0] if nome_registro else ""
    primeiro_config = nome_config.split()[0] if nome_config else ""

    return bool(
        primeiro_registro
        and primeiro_registro == primeiro_config
    )


def criar_mapa_ultimo_historico(historico):
    mapa = {}

    for registro in historico:
        idprocesso = str(registro.get("idprocesso", "")).strip()
        data = registro.get("data_ultimo_historico")

        if not idprocesso or valor_vazio(data):
            continue

        data_convertida = converter_data(data)
        if not data_convertida:
            continue

        mapa[idprocesso] = data_convertida

    return mapa


def obter_processos_acelerados_agenda(agenda, hoje):
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
        tipo = registro.get(coluna_tipo)
        tipo_texto = normalizar_texto(tipo)
        tipo_id = inteiro(tipo)

        if not (
            tipo_id == 3
            or "aceler" in tipo_texto
        ):
            continue

        data_agenda = converter_data(registro.get(coluna_data))
        if data_agenda != hoje.date():
            continue

        processo = normalizar_processo(registro.get(coluna_processo))
        if processo:
            acelerados.add(processo)

    return acelerados


def obter_ultima_aceleracao_por_processo(controle):
    """
    Retorna a data da última aceleração válida de cada número de processo.

    A chave é o número CNJ normalizado, independentemente do idprocesso/pasta.
    Isso evita que pastas duplicadas do mesmo processo sejam tratadas como
    processos diferentes.

    Registros com ERRO_IILEX ou CANCELADO não contam como aceleração válida.
    RESERVADO é mantido como bloqueio conservador para evitar duplicação caso
    uma execução tenha sido interrompida entre o POST no IILEX e a atualização
    final do controle.
    """
    ultimas = {}

    for registro in controle:
        status = normalizar_texto(registro.get("status"))

        if status in {
            "erro iilex",
            "cancelado",
        }:
            continue

        processo = normalizar_processo(
            registro.get("processo")
        )
        data_agendamento = converter_data(
            registro.get("data_agendamento")
        )

        if not processo or not data_agendamento:
            continue

        data_atual = ultimas.get(processo)
        if data_atual is None or data_agendamento > data_atual:
            ultimas[processo] = data_agendamento

    return ultimas


def criar_mapa_processos_canonicos(
    contencioso,
    mapa_historico,
    coluna_processo,
    coluna_idprocesso,
    coluna_status,
):
    """
    Consolida duplicidades do Contencioso por número CNJ.

    Para cada número de processo, considera apenas pastas ATIVAS e escolhe
    como pasta canônica aquela que possui o histórico mais recente.
    Em empate de data, usa o maior idprocesso como desempate determinístico.

    A regra é conservadora: se existe uma pasta ativa do mesmo CNJ com
    movimentação mais recente, uma cópia antiga/stagnada não pode fazer o
    processo parecer parado e gerar uma aceleração indevida.
    """
    canonicos = {}

    for registro in contencioso:
        if not status_ativo(registro.get(coluna_status)):
            continue

        processo_original = str(
            registro.get(coluna_processo, "")
        ).strip()
        processo = normalizar_processo(processo_original)

        if not processo:
            continue

        idprocesso = str(
            registro.get(coluna_idprocesso, "")
        ).strip()

        if not idprocesso:
            continue

        data_ultimo_historico = mapa_historico.get(idprocesso)
        if not data_ultimo_historico:
            continue

        id_numerico = inteiro(idprocesso)
        chave_desempate = (
            data_ultimo_historico,
            id_numerico if id_numerico is not None else -1,
            idprocesso,
        )

        atual = canonicos.get(processo)

        if atual is None or chave_desempate > atual["chave_desempate"]:
            canonicos[processo] = {
                "registro": registro,
                "processo": processo,
                "processo_original": processo_original,
                "idprocesso": idprocesso,
                "data_ultimo_historico": data_ultimo_historico,
                "chave_desempate": chave_desempate,
            }

    return canonicos


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
        obrigatoria=coluna_id_responsavel is None,
    )

    mapa_historico = criar_mapa_ultimo_historico(historico)

    # Bloqueio adicional para o próprio dia usando a Agenda do IILEX.
    acelerados_agenda_hoje = obter_processos_acelerados_agenda(
        agenda,
        hoje,
    )

    # Última aceleração conhecida por número CNJ, independentemente da pasta.
    ultima_aceleracao = obter_ultima_aceleracao_por_processo(
        controle
    )

    # Consolida as várias pastas do mesmo CNJ em uma única pasta canônica.
    processos_canonicos = criar_mapa_processos_canonicos(
        contencioso,
        mapa_historico,
        coluna_processo,
        coluna_idprocesso,
        coluna_status,
    )

    quantidade = int(
        configuracao.get(
            "quantidade_por_responsavel",
            2,
        )
    )

    resultado = {}
    selecionados_no_lote = set()

    for responsavel in configuracao.get("responsaveis", []):
        nome = responsavel.get("nome")

        if not responsavel.get("ativo", True):
            resultado[nome] = []
            continue

        if nome in indisponiveis:
            resultado[nome] = []
            continue

        candidatos = []

        for processo, canonico in processos_canonicos.items():
            registro = canonico["registro"]

            # Mantém a regra de negócio já existente de excluir Risco 4.
            if eh_risco_4(registro.get(coluna_risco)):
                continue

            if not responsavel_corresponde(
                registro,
                responsavel,
                coluna_id_responsavel,
                coluna_nome_responsavel,
            ):
                continue

            if processo in acelerados_agenda_hoje:
                continue

            data_ultimo_historico = canonico[
                "data_ultimo_historico"
            ]

            data_ultima_aceleracao = ultima_aceleracao.get(
                processo
            )

            # Regra central:
            # - se nunca foi acelerado, pode ser candidato;
            # - se já foi acelerado, só volta a ficar elegível depois de
            #   uma NOVA movimentação posterior à última aceleração.
            #
            # Assim, duplicidades de idprocesso não conseguem fazer o mesmo
            # CNJ reaparecer usando o histórico antigo de outra pasta.
            if (
                data_ultima_aceleracao
                and data_ultimo_historico <= data_ultima_aceleracao
            ):
                continue

            candidatos.append(
                {
                    "processo": processo,
                    "processo_original": canonico[
                        "processo_original"
                    ],
                    "idprocesso": canonico["idprocesso"],
                    "responsavel": nome,
                    "idusuario": responsavel.get("idusuario"),
                    "risco": registro.get(coluna_risco),
                    "status": registro.get(coluna_status),
                    "data_ultimo_historico": data_ultimo_historico,
                    "data_ultima_aceleracao": data_ultima_aceleracao,
                }
            )

        candidatos.sort(
            key=lambda item: item["data_ultimo_historico"]
        )

        escolhidos = []

        for item in candidatos:
            processo = item["processo"]

            if processo in selecionados_no_lote:
                continue

            escolhidos.append(item)
            selecionados_no_lote.add(processo)

            if len(escolhidos) >= quantidade:
                break

        resultado[nome] = escolhidos

    return resultado


def reservar_agendamento(supabase, item, hoje):
    registro = {
        "data_agendamento": hoje.strftime("%Y-%m-%d"),
        "processo": item["processo_original"],
        "idprocesso": item["idprocesso"],
        "responsavel": item["responsavel"],
        "idusuario": int(item["idusuario"]),
        "data_ultimo_historico": item["data_ultimo_historico"].strftime(
            "%Y-%m-%d"
        ),
        "status": "RESERVADO",
        "atualizado_em": hoje.isoformat(),
    }

    try:
        (
            supabase
            .table(TABELA_CONTROLE)
            .insert(registro)
            .execute()
        )
        return True
    except Exception as erro:
        texto = str(erro).lower()

        if (
            "duplicate" in texto
            or "unique" in texto
            or "23505" in texto
        ):
            return False

        raise


def atualizar_controle(
    supabase,
    item,
    hoje,
    status,
    status_http=None,
    retorno_iilex=None,
):
    dados = {
        "status": status,
        "status_http": status_http,
        "retorno_iilex": retorno_iilex,
        "atualizado_em": datetime.now(FUSO_BRASILIA).isoformat(),
    }

    (
        supabase
        .table(TABELA_CONTROLE)
        .update(dados)
        .eq(
            "data_agendamento",
            hoje.strftime("%Y-%m-%d"),
        )
        .eq(
            "processo",
            item["processo_original"],
        )
        .execute()
    )


def criar_agendamento_iilex(
    sessao,
    item,
    configuracao,
    hoje,
):
    agendamento = configuracao["agendamento"]
    data_texto = hoje.strftime("%Y-%m-%d")

    parametros = {
        "idmodulotabelaprocesso1": 1,
        "idprocesso1": item["idprocesso"],
        "idtabaux30": agendamento["id_agrupamento"],
        "idtipoagenda1": agendamento["id_tipo_compromisso"],
        "memo1": agendamento["descricao"],
        "idusuario1": item["idusuario"],
        "data1": data_texto,
        "data3": data_texto,
        "opcao1": "N",
        "texto10": item["processo_original"],
        "data4": data_texto,
    }

    url = f"{BASE_URL}/api/public/v1/insert/modulo/Agenda"

    ultima_resposta = None

    for tentativa in range(1, MAX_TENTATIVAS_POST + 1):
        try:
            resposta = sessao.post(
                url,
                params=parametros,
                timeout=30,
            )

            ultima_resposta = resposta

            if resposta.status_code in (200, 201):
                return True, resposta.status_code, resposta.text[:4000]

            if resposta.status_code == 429:
                if tentativa < MAX_TENTATIVAS_POST:
                    time.sleep(ESPERA_429)
                    continue

            if 500 <= resposta.status_code <= 599:
                if tentativa < MAX_TENTATIVAS_POST:
                    time.sleep(10)
                    continue

            return False, resposta.status_code, resposta.text[:4000]

        except requests.RequestException as erro:
            if tentativa < MAX_TENTATIVAS_POST:
                time.sleep(10)
                continue

            return False, None, str(erro)[:4000]

    if ultima_resposta is not None:
        return (
            False,
            ultima_resposta.status_code,
            ultima_resposta.text[:4000],
        )

    return False, None, "Falha sem resposta do IILEX."


def executar_agendamentos(
    supabase,
    sessao,
    selecionados,
    configuracao,
    indisponiveis,
    hoje,
):
    total_selecionado = sum(
        len(processos)
        for processos in selecionados.values()
    )

    criados = 0
    erros = 0
    ignorados = 0

    print()
    print("=" * 90)
    print("CRIAÇÃO DE ACELERAÇÕES")
    print("=" * 90)
    print(f"Data: {hoje.strftime('%d/%m/%Y')}")
    print(f"Selecionados: {total_selecionado}")
    print()

    for responsavel, processos in selecionados.items():
        if responsavel in indisponiveis:
            print(f"{responsavel}: INDISPONÍVEL")
            continue

        print(f"{responsavel}: {len(processos)} processo(s)")

        for item in processos:
            reservado = reservar_agendamento(
                supabase,
                item,
                hoje,
            )

            if not reservado:
                ignorados += 1
                print(
                    f"  IGNORADO | {item['processo_original']} | "
                    "já reservado/agendado hoje"
                )
                continue

            sucesso, status_http, retorno = criar_agendamento_iilex(
                sessao,
                item,
                configuracao,
                hoje,
            )

            if sucesso:
                atualizar_controle(
                    supabase,
                    item,
                    hoje,
                    status="CONCLUIDO",
                    status_http=status_http,
                    retorno_iilex=retorno,
                )

                criados += 1

                print(
                    f"  CRIADO | {item['processo_original']} | "
                    f"HTTP {status_http}"
                )
            else:
                atualizar_controle(
                    supabase,
                    item,
                    hoje,
                    status="ERRO_IILEX",
                    status_http=status_http,
                    retorno_iilex=retorno,
                )

                erros += 1

                print(
                    f"  ERRO | {item['processo_original']} | "
                    f"HTTP {status_http}"
                )

            time.sleep(INTERVALO_ENTRE_AGENDAMENTOS)

        print()

    print("-" * 90)
    print(f"CRIADOS: {criados}")
    print(f"IGNORADOS: {ignorados}")
    print(f"ERROS: {erros}")
    print("-" * 90)

    if erros:
        raise RuntimeError(
            f"{erros} agendamento(s) falharam no IILEX."
        )


def main():
    configuracao = carregar_configuracao()
    hoje = datetime.now(FUSO_BRASILIA)

    executar, motivo = data_execucao_valida(
        configuracao,
        hoje,
    )

    if not executar:
        print(f"Execução ignorada: {motivo}.")
        return

    print("Lendo indisponibilidades...")
    indisponiveis = carregar_indisponibilidades(hoje)

    print(
        "Indisponíveis hoje: "
        + (
            ", ".join(sorted(indisponiveis))
            if indisponiveis
            else "nenhum"
        )
    )

    supabase = criar_supabase()
    sessao_iilex = criar_sessao_iilex()

    print("Carregando Contencioso...")
    contencioso = ler_tabela(
        supabase,
        TABELA_CONTENCIOSO,
    )
    print(f"Contencioso: {len(contencioso):,}")

    print("Carregando último Histórico...")
    historico = ler_tabela(
        supabase,
        TABELA_ULTIMO_HISTORICO,
    )
    print(f"Último Histórico: {len(historico):,}")

    print("Carregando Agenda...")
    agenda = ler_tabela(
        supabase,
        TABELA_AGENDA,
    )
    print(f"Agenda: {len(agenda):,}")

    print("Carregando controle...")
    controle = ler_tabela(
        supabase,
        TABELA_CONTROLE,
    )
    print(f"Controle: {len(controle):,}")

    selecionados = selecionar_processos(
        contencioso,
        historico,
        agenda,
        controle,
        configuracao,
        indisponiveis,
        hoje,
    )

    executar_agendamentos(
        supabase,
        sessao_iilex,
        selecionados,
        configuracao,
        indisponiveis,
        hoje,
    )


if __name__ == "__main__":
    main()