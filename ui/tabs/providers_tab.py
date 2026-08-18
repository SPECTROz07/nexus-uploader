# ui/tabs/providers_tab.py
"""
Aba de Providers no estilo das extensoes do Mihon: cada provider aparece com a
logo do site, o dominio e quantos bots o usam.

A lista e montada estaticamente (core.provider_registry), sem importar nenhum
provider - 20 deles sobem navegador no __init__ e abrir a aba nao pode custar
isso. As logos vem do cache em assets/provider_icons; baixar as que faltam e
uma acao explicita do usuario, feita fora da thread da UI.
"""
from pathlib import Path

from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QIcon, QPixmap, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QSizePolicy, QFrame,
    QDialog, QDialogButtonBox, QFormLayout, QPlainTextEdit, QMessageBox,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QMenu,
)

from ui.theme import AppTheme
from ui.icons import get_icon
from core.provider_registry import listar_providers
from core.provider_icons import caminho_icone, obter_icone, ler_origem
from core.provider_health import carregar_cache, sondar_todos, ROTULOS
from core.git_updater import GitUpdater
from core import provider_store


def cor(*nomes, padrao="#888888"):
    """
    Devolve a primeira cor que existir no AppTheme.

    Este arquivo roda em dois projetos com paletas diferentes: um tem os nomes
    novos (BG_ELEVATED, TEXT_SECONDARY, ERROR...), o outro so os legados
    (TERTIARY_COLOR, MUTED_TEXT_COLOR, ERROR_COLOR...). Resolver por tentativa
    evita manter duas copias do mesmo arquivo divergindo com o tempo.
    """
    for nome in nomes:
        valor = getattr(AppTheme, nome, None)
        if valor:
            return valor
    return padrao


COR_FUNDO_ELEVADO = cor("BG_ELEVATED", "TERTIARY_COLOR", padrao="#18181f")
COR_TEXTO = cor("TEXT_PRIMARY", "TEXT_COLOR", padrao="#f0f0f5")
COR_TEXTO_FRACO = cor("TEXT_SECONDARY", "MUTED_TEXT_COLOR", padrao="#9090a8")
COR_TEXTO_APAGADO = cor("TEXT_MUTED", "MUTED_TEXT_COLOR", padrao="#55556a")
COR_ERRO = cor("ERROR", "ERROR_COLOR", padrao="#e05555")
COR_AVISO = cor("WARNING", "ACCENT_COLOR", padrao="#e0b855")
COR_DESTAQUE = cor("ACCENT", "ACCENT_COLOR", padrao="#c8a96e")

# Cards da lista: fundo levemente elevado sobre a janela, com borda sutil.
COR_CARD = cor("BG_SURFACE", "SECONDARY_COLOR", padrao="#1a1a1a")
COR_CARD_HOVER = cor("BG_HOVER", "HOVER_COLOR", padrao="#2a2a2a")
COR_BORDA = cor("BORDER", padrao="#2a2a2a")
COR_BORDA_FORTE = cor("BORDER_LIGHT", "HOVER_COLOR", padrao="#3a3a3a")

TAM_ICONE = 40
# O card e um QFrame DENTRO do widget da linha (nao o ::item do QListWidget):
# assim logo, texto e selos ficam obrigatoriamente dentro da caixa desenhada.
# ALTURA_CARD e a caixa visivel; ALTURA_LINHA soma as margens externas.
ALTURA_CARD = 56
ALTURA_LINHA = ALTURA_CARD + 8


class BaixadorDeLogos(QThread):
    """Busca as logos que faltam sem travar a interface."""
    progresso = Signal(int, int, str)
    terminou = Signal(int)

    def __init__(self, providers, forcar=False, parent=None):
        super().__init__(parent)
        self.providers = providers
        self.forcar = forcar

    def run(self):
        total = len(self.providers)
        baixados = 0
        for i, p in enumerate(self.providers, 1):
            self.progresso.emit(i, total, p.nome)
            try:
                obter_icone(p.nome, p.site_publico, forcar=self.forcar)
                baixados += 1
            except Exception:
                pass
        self.terminou.emit(baixados)


class SondadorDeSaude(QThread):
    """Sonda os sites de todos os providers sem travar a interface."""
    progresso = Signal(int, int, str)
    terminou = Signal(dict)

    def __init__(self, providers, parent=None):
        super().__init__(parent)
        self.providers = providers

    def run(self):
        resultados = sondar_todos(self.providers, progresso=self.progresso.emit)
        self.terminou.emit(resultados)


class TestadorDeProvider(QThread):
    """Carrega o provider de verdade e busca os capitulos de uma URL de obra."""
    terminou = Signal(bool, str)

    def __init__(self, nome, url, parent=None):
        super().__init__(parent)
        self.nome = nome
        self.url = url

    def run(self):
        import importlib
        import time as _t
        try:
            inicio = _t.time()
            module = importlib.import_module(f"providers.{self.nome}_provider")
            esperada = "".join(p.capitalize() for p in self.nome.split('_')) + "Provider"
            classe = getattr(module, esperada, None)
            if classe is None:
                candidatas = [obj for attr in dir(module)
                              if attr.endswith("Provider")
                              and attr not in ("BaseProvider", "ScanApiProvider")
                              for obj in [getattr(module, attr)]
                              if isinstance(obj, type) and obj.__module__ == module.__name__]
                if not candidatas:
                    raise AttributeError("nenhuma classe *Provider no arquivo")
                classe = candidatas[0]
            provider = classe()
            capitulos = provider.get_chapters(self.url) or []
            duracao = _t.time() - inicio
            linhas = [f"{len(capitulos)} capitulo(s) em {duracao:.1f}s  (classe {classe.__name__})", ""]
            for c in capitulos[:60]:
                numero = c.get('number', '?')
                titulo = c.get('title') or ''
                linhas.append(f"  cap {numero}  {titulo}".rstrip())
            if len(capitulos) > 60:
                linhas.append(f"  ... e mais {len(capitulos) - 60}")
            self.terminou.emit(True, "\n".join(linhas))
        except Exception as e:
            self.terminou.emit(False, f"{type(e).__name__}: {e}")


class DialogoTestarProvider(QDialog):
    """Cola a URL de uma obra e ve o que o provider devolve, sem sair da GUI."""

    def __init__(self, nome_provider, parent=None):
        super().__init__(parent)
        self.nome_provider = nome_provider
        self.testador = None
        self.setWindowTitle(f"Testar Provider: {nome_provider}")
        self.setMinimumSize(640, 420)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://site.com/manga/nome-da-obra")
        form.addRow("URL da obra:", self.url_input)
        layout.addLayout(form)

        self.botao_testar = QPushButton("Buscar Capítulos")
        self.botao_testar.clicked.connect(self._testar)
        layout.addWidget(self.botao_testar)

        self.saida = QPlainTextEdit()
        self.saida.setReadOnly(True)
        self.saida.setPlaceholderText(
            "O provider é carregado de verdade — se ele usa navegador "
            "(Playwright/Chrome), a primeira busca pode demorar.")
        layout.addWidget(self.saida)

        fechar = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        fechar.rejected.connect(self.reject)
        layout.addWidget(fechar)

    def _testar(self):
        url = self.url_input.text().strip()
        if not url:
            self.saida.setPlainText("Cole a URL de uma obra do site deste provider.")
            return
        if self.testador and self.testador.isRunning():
            return
        self.botao_testar.setEnabled(False)
        self.saida.setPlainText("Buscando capítulos...")
        self.testador = TestadorDeProvider(self.nome_provider, url, parent=self)
        self.testador.terminou.connect(self._fim)
        self.testador.start()

    def _fim(self, ok, texto):
        self.botao_testar.setEnabled(True)
        prefixo = "" if ok else "FALHOU\n\n"
        self.saida.setPlainText(prefixo + texto)


class DialogoNovoScanApi(QDialog):
    """Formulario dos 3 campos que um site novo da engine ScanApi precisa."""

    def __init__(self, pasta_providers, parent=None):
        super().__init__(parent)
        self.pasta = pasta_providers
        self.arquivo_criado = None
        self.setWindowTitle("Novo Provider (engine ScanApi)")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        aviso = QLabel(
            "Para sites que rodam a API \"scan\" (payload obr_id/cap_id).\n"
            "Confira antes se <api>/obras/catalogo responde JSON.")
        aviso.setWordWrap(True)
        layout.addWidget(aviso)

        form = QFormLayout()
        self.nome_input = QLineEdit()
        self.nome_input.setPlaceholderText("maidscan (minusculas, sem espaço)")
        self.rotulo_input = QLineEdit()
        self.rotulo_input.setPlaceholderText("Maid Scan (nome de exibição)")
        self.base_input = QLineEdit()
        self.base_input.setPlaceholderText("https://site.com")
        self.api_input = QLineEdit()
        self.api_input.setPlaceholderText("https://api.site.com")
        self.cdn_input = QLineEdit()
        self.cdn_input.setPlaceholderText("https://cdn.site.com (opcional)")
        form.addRow("Nome do arquivo:", self.nome_input)
        form.addRow("Rótulo:", self.rotulo_input)
        form.addRow("base_url:", self.base_input)
        form.addRow("api_url:", self.api_input)
        form.addRow("cdn_url:", self.cdn_input)
        layout.addLayout(form)

        botoes = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        botoes.accepted.connect(self._criar)
        botoes.rejected.connect(self.reject)
        layout.addWidget(botoes)

    def _criar(self):
        import re as _re
        nome = self.nome_input.text().strip().lower()
        base = self.base_input.text().strip().rstrip('/')
        api = self.api_input.text().strip().rstrip('/')
        cdn = self.cdn_input.text().strip().rstrip('/')
        rotulo = self.rotulo_input.text().strip() or nome

        if not _re.fullmatch(r'[a-z0-9_]+', nome or ''):
            QMessageBox.warning(self, "Nome inválido", "Use só minúsculas, números e _ (ex.: maid_scan).")
            return
        if not base.startswith('http') or not api.startswith('http'):
            QMessageBox.warning(self, "URL inválida", "base_url e api_url precisam começar com http(s)://")
            return
        destino = self.pasta / f"{nome}_provider.py"
        if destino.exists():
            QMessageBox.warning(self, "Já existe", f"O arquivo {destino.name} já existe.")
            return

        classe = "".join(p.capitalize() for p in nome.split('_')) + "Provider"
        linhas = [
            f"# providers/{nome}_provider.py",
            f'"""Provider para {rotulo} ({base.split("//", 1)[-1]}) — engine ScanApi."""',
            "from .scanapi_base import ScanApiProvider",
            "",
            "",
            f"class {classe}(ScanApiProvider):",
            f'    base_url = "{base}"',
            f'    api_url = "{api}"',
        ]
        if cdn:
            linhas.append(f'    cdn_url = "{cdn}"')
        linhas.append(f'    rotulo = "{rotulo}"')
        linhas.append("")
        destino.write_text("\n".join(linhas), encoding="utf-8")
        self.arquivo_criado = destino
        self.accept()


class SincronizadorDeRepo(QThread):
    """Consulta a pasta providers/ do repositorio e compara com a local."""
    terminou = Signal(list)
    falhou = Signal(str)

    def __init__(self, pasta_local, parent=None):
        super().__init__(parent)
        self.pasta_local = pasta_local

    def run(self):
        try:
            cfg = GitUpdater().load_config()
            if not cfg.get("repo_url"):
                raise RuntimeError("Repositório não configurado — preencha a URL do Git nas Configurações.")
            remotos = provider_store.listar_remotos(
                cfg["repo_url"], cfg.get("token", ""), cfg.get("branch", "main"))
            self.terminou.emit(provider_store.comparar(self.pasta_local, remotos))
        except Exception as e:
            self.falhou.emit(str(e))


class BaixadorDeRepo(QThread):
    """Baixa do repositorio os arquivos escolhidos, um a um."""
    progresso = Signal(int, int, str)
    terminou = Signal(int, list)  # baixados, falhas

    def __init__(self, itens, pasta_local, parent=None):
        super().__init__(parent)
        self.itens = itens
        self.pasta_local = pasta_local

    def run(self):
        cfg = GitUpdater().load_config()
        baixados, falhas = 0, []
        for i, item in enumerate(self.itens, 1):
            self.progresso.emit(i, len(self.itens), item["name"])
            try:
                provider_store.baixar_arquivo(
                    cfg["repo_url"], cfg.get("token", ""), item["remoto"]["path"],
                    self.pasta_local / item["name"], cfg.get("branch", "main"))
                baixados += 1
            except Exception as e:
                falhas.append(f"{item['name']}: {e}")
        self.terminou.emit(baixados, falhas)


class DialogoRepositorio(QDialog):
    """Novos e atualizações de providers direto do repositorio do GitHub."""

    ROTULO_ESTADO = {
        "novo": ("novo no repo", None),
        "atualizacao": ("atualização", None),
        "so-local": ("só nesta máquina", None),
        "em-dia": ("em dia", None),
    }

    def __init__(self, pasta_local, parent=None):
        super().__init__(parent)
        self.pasta_local = pasta_local
        self.itens = []
        self.trabalhador = None
        self.baixou_algo = False
        self.setWindowTitle("Providers do Repositório")
        self.setMinimumSize(560, 460)

        layout = QVBoxLayout(self)
        self.aviso = QLabel("Consultando o repositório...")
        self.aviso.setWordWrap(True)
        layout.addWidget(self.aviso)

        self.arvore = QTreeWidget()
        self.arvore.setHeaderLabels(["Arquivo", "Estado"])
        self.arvore.setRootIsDecorated(False)
        self.arvore.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.arvore.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.arvore)

        self.barra = QProgressBar()
        self.barra.setVisible(False)
        layout.addWidget(self.barra)

        linha_botoes = QHBoxLayout()
        self.b_baixar = QPushButton("Baixar Marcados")
        self.b_baixar.setEnabled(False)
        self.b_baixar.clicked.connect(self._baixar)
        linha_botoes.addWidget(self.b_baixar)
        linha_botoes.addStretch()
        b_fechar = QPushButton("Fechar")
        b_fechar.clicked.connect(self.accept)
        linha_botoes.addWidget(b_fechar)
        layout.addLayout(linha_botoes)

        self._consultar()

    def _consultar(self):
        self.trabalhador = SincronizadorDeRepo(self.pasta_local, parent=self)
        self.trabalhador.terminou.connect(self._mostrar)
        self.trabalhador.falhou.connect(self._erro)
        self.trabalhador.start()

    def _erro(self, msg):
        self.aviso.setText(f"Falha ao consultar o repositório:\n{msg}")

    def _mostrar(self, itens):
        self.itens = itens
        self.arvore.clear()
        novos = sum(1 for i in itens if i["estado"] == "novo")
        atualiz = sum(1 for i in itens if i["estado"] == "atualizacao")
        if novos or atualiz:
            self.aviso.setText(f"{novos} provider(s) novo(s) e {atualiz} atualização(ões) disponíveis. "
                               "Marque o que quiser baixar.")
        else:
            self.aviso.setText("Tudo em dia com o repositório.")
        for item in itens:
            no = QTreeWidgetItem([item["name"], self.ROTULO_ESTADO[item["estado"]][0]])
            no.setData(0, Qt.ItemDataRole.UserRole, item)
            if item["estado"] in ("novo", "atualizacao"):
                no.setFlags(no.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                no.setCheckState(0, Qt.CheckState.Checked)
            self.arvore.addTopLevelItem(no)
        self.b_baixar.setEnabled(bool(novos or atualiz))

    def _marcados(self):
        marcados = []
        for i in range(self.arvore.topLevelItemCount()):
            no = self.arvore.topLevelItem(i)
            if (no.flags() & Qt.ItemFlag.ItemIsUserCheckable
                    and no.checkState(0) == Qt.CheckState.Checked):
                marcados.append(no.data(0, Qt.ItemDataRole.UserRole))
        return marcados

    def _baixar(self):
        marcados = self._marcados()
        if not marcados:
            return
        self.b_baixar.setEnabled(False)
        self.barra.setVisible(True)
        self.barra.setRange(0, len(marcados))
        self.trabalhador = BaixadorDeRepo(marcados, self.pasta_local, parent=self)
        self.trabalhador.progresso.connect(
            lambda a, t, n: (self.barra.setValue(a), self.barra.setFormat(f"Baixando {a}/{t} - {n}")))
        self.trabalhador.terminou.connect(self._fim_download)
        self.trabalhador.start()

    def _fim_download(self, baixados, falhas):
        self.barra.setVisible(False)
        self.baixou_algo = self.baixou_algo or baixados > 0
        if falhas:
            QMessageBox.warning(self, "Falhas no download", "\n".join(falhas))
        self.aviso.setText(f"{baixados} arquivo(s) baixado(s). Consultando de novo...")
        self._consultar()


class LinhaProvider(QWidget):
    """Uma linha da lista: logo, nome, dominio e selos de aviso."""

    def __init__(self, info, bots_usando=0, generico=False, saude=None, parent=None):
        super().__init__(parent)
        self.setFixedHeight(ALTURA_LINHA)
        # sem Expanding a linha fica na largura minima e os selos grudam no nome
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        # o tema global pinta QWidget com fundo solido; transparente aqui, o
        # card visivel e o QFrame interno
        self.setStyleSheet("background: transparent;")

        externo = QVBoxLayout(self)
        externo.setContentsMargins(2, 4, 6, 4)
        externo.setSpacing(0)

        self.card = QFrame()
        self.card.setObjectName("CardProvider")
        self.card.setFixedHeight(ALTURA_CARD)
        externo.addWidget(self.card)

        layout = QHBoxLayout(self.card)
        layout.setContentsMargins(10, 0, 10, 0)
        layout.setSpacing(12)

        logo = QLabel()
        logo.setFixedSize(TAM_ICONE, TAM_ICONE)
        caminho = caminho_icone(info.nome)
        if caminho.exists():
            pix = QPixmap(str(caminho)).scaled(
                TAM_ICONE, TAM_ICONE,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            logo.setPixmap(pix)
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo.setStyleSheet("background: transparent; border: none;")
        else:
            logo.setText(info.nome_exibicao[:1].upper() or "?")
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
            logo.setStyleSheet(
                f"background-color: {COR_FUNDO_ELEVADO};"
                f"color: {COR_TEXTO_APAGADO}; border-radius: 6px; border: none;")
        layout.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)

        texto = QVBoxLayout()
        texto.setSpacing(2)
        texto.setContentsMargins(0, 0, 0, 0)

        nome = QLabel(info.nome_exibicao)
        fonte = QFont()
        fonte.setBold(True)
        fonte.setPointSize(10)
        nome.setFont(fonte)
        nome.setStyleSheet("background: transparent; border: none;")
        texto.addWidget(nome)

        detalhe = info.dominio or "site nao detectado"
        sub = QLabel(detalhe)
        sub.setStyleSheet(f"color: {COR_TEXTO_FRACO}; font-size: 8pt; background: transparent; border: none;")
        texto.addWidget(sub)

        layout.addLayout(texto, 1)
        layout.setAlignment(texto, Qt.AlignmentFlag.AlignVCenter)

        # nome diferente de `cor` de proposito: aquele e a funcao que resolve a paleta
        for rotulo, cor_selo, dica in self._selos(info, bots_usando, generico, saude):
            selo = QLabel(rotulo)
            selo.setToolTip(dica)
            selo.setFixedHeight(22)
            selo.setStyleSheet(
                f"color: {cor_selo}; border: 1px solid {cor_selo}; border-radius: 11px;"
                f"padding: 0px 10px; font-size: 8pt; background-color: transparent;")
            layout.addWidget(selo, 0, Qt.AlignmentFlag.AlignVCenter)

    def marcar_selecao(self, selecionado):
        """Reflete a selecao do QListWidget na borda do card interno."""
        self.card.setProperty("selecionado", "true" if selecionado else "false")
        self.card.style().unpolish(self.card)
        self.card.style().polish(self.card)

    @staticmethod
    def _selos(info, bots_usando, generico, saude=None):
        # Nome de classe fora da convencao deixou de ser problema: o
        # load_provider do run_bot acha a classe Provider do arquivo mesmo
        # quando a capitalizacao nao bate, entao nao ha mais selo pra isso.
        selos = []
        if saude:
            rotulo, gravidade = ROTULOS.get(saude.get("status"), ("", "ok"))
            if rotulo:
                cor_saude = COR_ERRO if gravidade == "erro" else COR_AVISO
                detalhe = saude.get("detalhe") or ""
                selos.append((rotulo, cor_saude,
                              f"Última sondagem: {detalhe}".strip()))
        if not info.site:
            selos.append((
                "sem site", COR_AVISO,
                "Nao foi possivel achar a URL do site no codigo do provider."))
        elif generico:
            selos.append((
                "sem logo", COR_TEXTO_APAGADO,
                "O site nao respondeu com favicon; o icone e gerado a partir da inicial."))
        if bots_usando:
            selos.append((
                f"{bots_usando} obras", COR_DESTAQUE,
                f"{bots_usando} obras monitoradas usam este provider."))
        return selos


class ProvidersTab(QWidget):
    """Aba de gerenciamento de providers."""

    def __init__(self, ao_importar=None, ao_abrir_pasta=None, ao_editar=None,
                 contagem_por_provider=None, parent=None):
        super().__init__(parent)
        self.ao_importar = ao_importar
        self.ao_abrir_pasta = ao_abrir_pasta
        self.ao_editar = ao_editar
        self.contagem_por_provider = contagem_por_provider or (lambda: {})
        self.providers = []
        self.baixador = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        topo = QHBoxLayout()
        titulo = QLabel("Providers")
        titulo.setObjectName("TitleLabel")
        topo.addWidget(titulo)
        topo.addStretch()

        self.resumo = QLabel("")
        self.resumo.setStyleSheet(f"color: {COR_TEXTO_FRACO};")
        topo.addWidget(self.resumo)
        layout.addLayout(topo)

        busca_linha = QHBoxLayout()
        self.busca = QLineEdit()
        self.busca.setObjectName("ProvidersSearch")
        self.busca.setPlaceholderText("Filtrar por nome ou site...")
        self.busca.setClearButtonEnabled(True)
        self.busca.textChanged.connect(self._filtrar)
        busca_linha.addWidget(self.busca)
        layout.addLayout(busca_linha)

        self.lista = QListWidget()
        self.lista.setObjectName("ProvidersList")
        self.lista.setIconSize(QSize(TAM_ICONE, TAM_ICONE))
        self.lista.setSpacing(0)
        self.lista.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.lista.itemDoubleClicked.connect(self._editar_selecionado)
        self.lista.currentItemChanged.connect(self._refletir_selecao)
        self.lista.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lista.customContextMenuRequested.connect(self._menu_contexto)
        layout.addWidget(self.lista)

        # Estilo proprio da aba: sobrescreve o tema global (que pinta itens com
        # padding grosso e borda de foco vermelha). Cada item vira um card limpo.
        self.setStyleSheet(f"""
            QLineEdit#ProvidersSearch {{
                background-color: {COR_CARD};
                border: 1px solid {COR_BORDA};
                border-radius: 8px;
                padding: 10px 12px;
            }}
            QLineEdit#ProvidersSearch:focus {{
                border: 1px solid {COR_BORDA_FORTE};
            }}
            QListWidget#ProvidersList {{
                background: transparent;
                border: none;
                padding: 0px;
                outline: none;
            }}
            QListWidget#ProvidersList::item {{
                background: transparent;
                border: none;
                margin: 0px;
                padding: 0px;
            }}
            QListWidget#ProvidersList::item:hover,
            QListWidget#ProvidersList::item:selected {{
                background: transparent;
                border: none;
            }}
            QFrame#CardProvider {{
                background-color: {COR_CARD};
                border: 1px solid {COR_BORDA};
                border-radius: 10px;
            }}
            QFrame#CardProvider:hover {{
                background-color: {COR_CARD_HOVER};
                border: 1px solid {COR_BORDA_FORTE};
            }}
            QFrame#CardProvider[selecionado="true"] {{
                background-color: {COR_CARD_HOVER};
                border: 1px solid {COR_DESTAQUE};
            }}
            QPushButton {{
                background-color: {COR_CARD};
                border: 1px solid {COR_BORDA};
                border-radius: 6px;
                padding: 5px 12px;
                font-weight: normal;
            }}
            QPushButton:hover {{
                background-color: {COR_CARD_HOVER};
                border: 1px solid {COR_BORDA_FORTE};
            }}
        """)

        self.barra = QProgressBar()
        self.barra.setVisible(False)
        layout.addWidget(self.barra)

        botoes = QHBoxLayout()
        botoes.setSpacing(8)

        def _botao(texto, icone, cor_icone):
            b = QPushButton(" " + texto)
            b.setIcon(get_icon(icone, cor_icone))
            b.setIconSize(QSize(15, 15))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            return b

        b_importar = _botao("Importar Provider", "file-plus", COR_TEXTO)
        b_importar.clicked.connect(lambda: self._chamar(self.ao_importar))

        b_editar = _botao("Editar", "edit", COR_TEXTO)
        b_editar.clicked.connect(lambda: self._chamar(self.ao_editar))

        b_pasta = _botao("Abrir Pasta", "folder", COR_TEXTO)
        b_pasta.clicked.connect(lambda: self._chamar(self.ao_abrir_pasta))

        b_novo_scanapi = _botao("Novo ScanApi", "plus", COR_TEXTO)
        b_novo_scanapi.clicked.connect(self._novo_scanapi)

        self.b_repo = _botao("Repositório", "download-cloud", COR_DESTAQUE)
        self.b_repo.clicked.connect(self._abrir_repositorio)

        self.b_testar = _botao("Testar", "play", COR_TEXTO)
        self.b_testar.clicked.connect(self._testar_selecionado)

        self.b_saude = _botao("Verificar Saúde", "activity", COR_DESTAQUE)
        self.b_saude.clicked.connect(self._sondar_saude)

        self.b_logos = _botao("Baixar Logos", "download", COR_DESTAQUE)
        self.b_logos.clicked.connect(self._baixar_faltantes)

        b_recarregar = _botao("Recarregar", "refresh-cw", COR_TEXTO)
        b_recarregar.clicked.connect(self.recarregar)

        # botao sem acao ligada nao aparece: um dos projetos nao tem editor de
        # provider, e botao que nao faz nada e pior do que botao ausente
        for botao, acao in ((b_importar, self.ao_importar),
                            (b_editar, self.ao_editar),
                            (b_pasta, self.ao_abrir_pasta)):
            if acao:
                botoes.addWidget(botao)
            else:
                botao.setParent(None)

        botoes.addWidget(b_novo_scanapi)
        botoes.addWidget(self.b_repo)
        botoes.addWidget(self.b_testar)
        botoes.addStretch()
        botoes.addWidget(self.b_saude)
        botoes.addWidget(self.b_logos)
        botoes.addWidget(b_recarregar)
        layout.addLayout(botoes)

        self.sondador = None
        self.recarregar()

    # ------------------------------------------------------------------
    def recarregar(self):
        self.providers = listar_providers()
        origem = ler_origem()
        contagem = self.contagem_por_provider()
        saude_cache = carregar_cache()

        self.lista.clear()
        for info in self.providers:
            item = QListWidgetItem(self.lista)
            linha = LinhaProvider(
                info,
                bots_usando=contagem.get(info.nome, 0),
                generico=(origem.get(info.nome) == 'fallback'),
                saude=saude_cache.get(info.nome),
            )
            item.setSizeHint(QSize(self.lista.viewport().width(), ALTURA_LINHA))
            item.setData(Qt.ItemDataRole.UserRole, info.nome)
            self.lista.addItem(item)
            self.lista.setItemWidget(item, linha)

        com_logo = sum(1 for i in self.providers if origem.get(i.nome) == 'site')
        recado = f"{len(self.providers)} providers · {com_logo} com logo do site"
        problemas = sum(1 for i in self.providers
                        if ROTULOS.get(saude_cache.get(i.nome, {}).get("status"), ("", "ok"))[1] != "ok")
        if problemas:
            recado += f" · {problemas} com problema de site"
        self.resumo.setText(recado)
        self._filtrar(self.busca.text())

    def resizeEvent(self, evento):
        """
        A lista e montada no __init__, quando o widget ainda nao tem o tamanho
        final - as linhas nasceriam estreitas e os selos ficariam colados no nome.
        Aqui a largura de cada linha e reajustada pra acompanhar a viewport.
        """
        super().resizeEvent(evento)
        largura = self.lista.viewport().width()
        for i in range(self.lista.count()):
            item = self.lista.item(i)
            if item.sizeHint().width() != largura:
                item.setSizeHint(QSize(largura, ALTURA_LINHA))

    def _filtrar(self, texto):
        alvo = (texto or "").strip().lower()
        for i in range(self.lista.count()):
            item = self.lista.item(i)
            info = self.providers[i]
            visivel = (not alvo
                       or alvo in info.nome.lower()
                       or alvo in (info.site or "").lower()
                       or alvo in info.classe.lower())
            item.setHidden(not visivel)

    def provider_selecionado(self):
        item = self.lista.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _chamar(self, funcao):
        if funcao:
            funcao()

    def _editar_selecionado(self, _item=None):
        self._chamar(self.ao_editar)

    def _refletir_selecao(self, atual, anterior):
        """O ::item e transparente; a selecao aparece na borda do card interno."""
        for item, ligado in ((anterior, False), (atual, True)):
            if item is None:
                continue
            linha = self.lista.itemWidget(item)
            if linha is not None:
                linha.marcar_selecao(ligado)

    # ------------------------------------------------------------------
    @property
    def pasta_providers(self):
        from core.paths import pasta_providers
        return pasta_providers()

    def _menu_contexto(self, posicao):
        item = self.lista.itemAt(posicao)
        if not item:
            return
        self.lista.setCurrentItem(item)
        nome = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction(get_icon("play", COR_TEXTO), "Testar...").triggered.connect(self._testar_selecionado)
        if self.ao_editar:
            menu.addAction(get_icon("edit", COR_TEXTO), "Editar").triggered.connect(
                lambda: self._chamar(self.ao_editar))
        menu.addSeparator()
        menu.addAction(get_icon("trash-2", COR_ERRO), "Apagar Provider...").triggered.connect(
            lambda: self._apagar_provider(nome))
        menu.exec(self.lista.viewport().mapToGlobal(posicao))

    def _apagar_provider(self, nome):
        if not nome:
            return
        # No pacote compilado o provider é um .pyd (ex.: nome_provider.cp313-win_amd64.pyd)
        arquivos = sorted(self.pasta_providers.glob(f"{nome}_provider.py")) + \
            sorted(self.pasta_providers.glob(f"{nome}_provider*.pyd"))
        if not arquivos:
            QMessageBox.warning(self, "Apagar Provider", f"{nome}_provider não foi encontrado.")
            return
        em_uso = self.contagem_por_provider().get(nome, 0)
        detalhe = (f"\n\nATENÇÃO: {em_uso} obra(s) monitorada(s) usam este provider "
                   "e vão parar de verificar." if em_uso else "")
        nomes = ", ".join(a.name for a in arquivos)
        reply = QMessageBox.warning(
            self, "Apagar Provider",
            f"Apagar o arquivo <b>{nomes}</b> desta máquina?{detalhe}\n\n"
            "Se ele existir no repositório, dá pra baixar de novo pelo botão Repositório.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            for arquivo in arquivos:
                arquivo.unlink()
        except OSError as e:
            QMessageBox.critical(self, "Apagar Provider", f"Não deu pra apagar: {e}")
            return
        self.recarregar()

    def _abrir_repositorio(self):
        from ui.email_gate import garantir_acesso
        if not garantir_acesso(self):
            return
        dlg = DialogoRepositorio(self.pasta_providers, parent=self)
        dlg.exec()
        if dlg.baixou_algo:
            self.recarregar()

    def _testar_selecionado(self):
        nome = self.provider_selecionado()
        if not nome:
            QMessageBox.information(self, "Testar Provider",
                                    "Selecione um provider na lista primeiro.")
            return
        DialogoTestarProvider(nome, parent=self).exec()

    def _novo_scanapi(self):
        dlg = DialogoNovoScanApi(self.pasta_providers, parent=self)
        if dlg.exec() and dlg.arquivo_criado:
            self.recarregar()
            QMessageBox.information(
                self, "Provider criado",
                f"{dlg.arquivo_criado.name} criado.\nUse o botão Testar com a URL de uma obra pra validar.")

    def _sondar_saude(self):
        if self.sondador and self.sondador.isRunning():
            return
        self.b_saude.setEnabled(False)
        self.barra.setVisible(True)
        self.barra.setRange(0, len(self.providers))
        self.sondador = SondadorDeSaude(self.providers, parent=self)
        self.sondador.progresso.connect(self._progresso_saude)
        self.sondador.terminou.connect(self._fim_saude)
        self.sondador.start()

    def _progresso_saude(self, atual, total, nome):
        self.barra.setValue(atual)
        self.barra.setFormat(f"Sondando site {atual}/{total} - {nome}")

    def _fim_saude(self, _resultados):
        self.barra.setVisible(False)
        self.b_saude.setEnabled(True)
        self.recarregar()

    # ------------------------------------------------------------------
    def _baixar_faltantes(self):
        if self.baixador and self.baixador.isRunning():
            return
        origem = ler_origem()
        faltando = [p for p in self.providers
                    if not caminho_icone(p.nome).exists()
                    or origem.get(p.nome) == 'fallback']
        if not faltando:
            self.resumo.setText(self.resumo.text() + "  (nada faltando)")
            return

        self.b_logos.setEnabled(False)
        self.barra.setVisible(True)
        self.barra.setRange(0, len(faltando))

        self.baixador = BaixadorDeLogos(faltando, forcar=True, parent=self)
        self.baixador.progresso.connect(self._progresso_logo)
        self.baixador.terminou.connect(self._fim_logos)
        self.baixador.start()

    def _progresso_logo(self, atual, total, nome):
        self.barra.setValue(atual)
        self.barra.setFormat(f"Buscando logo {atual}/{total} - {nome}")

    def _fim_logos(self, _quantos):
        self.barra.setVisible(False)
        self.b_logos.setEnabled(True)
        self.recarregar()
