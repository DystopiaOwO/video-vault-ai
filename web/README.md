# video-vault-ai WebUI

開發模式：

```powershell
cd web
npm install
npm run dev
```

Python 後端仍跑在 `http://127.0.0.1:8765`，Vite 會 proxy `/api`。

打包後由 Python server 提供：

```powershell
cd web
npm run build
cd ..
python -m video_vault ui
```

若 `web/dist` 不存在，Python server 會 fallback 到舊版 UI；舊版也可從 `/classic` 開啟。
