# UI Prototype 測試方式

這個原型只使用 HTML 與 CSS，不會呼叫正式 API，也不會修改專案資料。

## 開啟方式

在 repository 的 `web` 目錄執行：

```powershell
npm ci
npm run dev
```

接著開啟：

```text
http://127.0.0.1:5173/ui-prototype.html
```

也可以直接用瀏覽器開啟 `web/ui-prototype.html`，但透過 Vite 測試比較接近正式 WebUI 的載入環境。

## 可測內容

- 左側工作區切換：儀表板、分鏡審核、調色與音訊、輸出
- 分鏡頁的片段列表與單一 Inspector 資訊層級
- 調色／音訊三欄工作區
- 1440px、1024px、768px、430px 等響應式寬度
- 長檔名、排除片段、待確認狀態與 disabled 輸出操作

原型中的表單與按鈕僅供版面及操作密度測試，不會儲存。

## 自動檢查

```powershell
npm test -- ui-prototype.test.ts
```

測試會確認：

- 原型有明確標示，不會被誤認為正式介面
- 四個工作區可透過 CSS 切換
- 分鏡只保留一套 Inspector 編輯模型
- 不包含假儲存空間、升級方案、無效快捷鍵或健康分數
- 具備桌面、平板與手機的響應式規則
