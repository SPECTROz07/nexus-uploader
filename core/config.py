# -*- coding: utf-8 -*-

# Host da API. Antes apontava pro .site (o SPA, que responde HTML e fazia o
# login estourar "Expecting value"); o backend real é o .com.
API_BASE_URL = "https://nexustoons.com"

# Chave de aplicação — o backend (OriginGuard) exige X-App-Key em requests
# server-to-server que não vêm de um Origin de navegador conhecido. Não é
# segredo forte (também aparece no bundle do site); mora aqui só pra passar o
# guarda. Pode ser sobrescrita por settings.json -> "app_key".
APP_KEY = "NxT_s3cur3_k3y_2026!xK9mPqL"
MAX_IMAGE_HEIGHT = 10000
PASS_THROUGH_EXTENSIONS = {'.webp', '.avif', '.jpeg'}
DEFAULT_WAIT_TIME_SECONDS = 180
INACTIVE_FOLDER_CHECK_INTERVAL_SECONDS = 600