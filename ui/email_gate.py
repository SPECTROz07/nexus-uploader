# ui/email_gate.py
"""
Portão de verificação por e-mail para as atualizações (app e providers).

A lista de e-mails aprovados mora no uploaders.json da RAIZ do repositório —
quem administra o repo decide quem baixa. O e-mail verificado fica salvo na
máquina e é revalidado contra o repo a cada uso (removeu do JSON, perdeu o
acesso); o modal só aparece quando não há e-mail aprovado salvo.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QMessageBox,
    QVBoxLayout,
)

from core.git_updater import GitUpdater
from core import provider_store


class DialogoVerificacaoEmail(QDialog):
    def __init__(self, parent=None, motivo=""):
        super().__init__(parent)
        self.setWindowTitle("Verificação de Uploader")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)

        titulo = QLabel("Confirme seu e-mail de uploader")
        fonte = QFont()
        fonte.setBold(True)
        fonte.setPointSize(11)
        titulo.setFont(fonte)
        layout.addWidget(titulo)

        texto = QLabel(
            "Digite o e-mail que foi aprovado no repositório da equipe.\n"
            "Sem ele, as atualizações do app e dos providers ficam bloqueadas.")
        texto.setWordWrap(True)
        layout.addWidget(texto)

        if motivo:
            aviso = QLabel(motivo)
            aviso.setWordWrap(True)
            aviso.setStyleSheet("color: #e0b855; font-size: 8pt;")
            layout.addWidget(aviso)

        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("seu-email@exemplo.com")
        email_atual = provider_store.email_salvo()
        if email_atual:
            self.email_input.setText(email_atual)
        layout.addWidget(self.email_input)

        botoes = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        botoes.accepted.connect(self.accept)
        botoes.rejected.connect(self.reject)
        layout.addWidget(botoes)

    def email(self):
        return self.email_input.text().strip().lower()


def garantir_acesso(parent=None) -> bool:
    """
    True se este uploader pode baixar atualizações. Valida o e-mail salvo em
    silêncio; abre o modal quando é preciso digitar (ou o salvo foi revogado).
    """
    cfg = GitUpdater().load_config()
    if not cfg.get("repo_url"):
        # sem repo configurado o download nem chega a acontecer — o fluxo
        # seguinte é quem avisa pra configurar; não é papel do portão
        return True

    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        ok, motivo = provider_store.verificar_acesso(
            cfg["repo_url"], cfg.get("token", ""), cfg.get("branch", "main"))
    finally:
        QApplication.restoreOverrideCursor()
    if ok:
        return True
    if motivo.startswith("Não deu pra checar"):
        QMessageBox.warning(parent, "Verificação de Uploader", motivo)
        return False

    # precisa digitar (primeira vez ou e-mail revogado)
    while True:
        dlg = DialogoVerificacaoEmail(parent, motivo="")
        if not dlg.exec():
            return False
        email = dlg.email()
        if not email:
            continue
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, motivo = provider_store.verificar_acesso(
                cfg["repo_url"], cfg.get("token", ""), cfg.get("branch", "main"), email=email)
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            provider_store.salvar_email(email)
            return True
        QMessageBox.warning(parent, "Verificação de Uploader", motivo)
