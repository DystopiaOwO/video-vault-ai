# React UI Prototype

這個頁面是 Video Vault AI 新版工作區的 React 可操作原型。

## 啟動

```powershell
cd web
npm ci
npm run dev
```

開啟：

```text
http://127.0.0.1:5173/ui-prototype-react.html
```

## 測試

```powershell
npm run test:prototype-react
npm run build
```

## 已包含

- 儀表板、分鏡、調色與音訊、核准與輸出工作區
- 單一片段列表搭配右側 Inspector
- 片段選取、納入、排除、鎖定與欄位編輯
- Before / After / Segment Preview 切換
- 調色與音訊控制分頁
- 人工確認與輸出 Gate
- 桌面、平板與手機響應式版面

## 安全邊界

此頁面只使用前端示範資料，不會呼叫正式 API、不會寫入專案，也不會建立 Render Job。完成 UI/UX 驗收後，再把元件接到既有 ProjectDetail、Storyboard、Color、Audio 與 Render Job API。
