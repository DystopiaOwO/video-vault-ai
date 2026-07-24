# Third-Party Notices

本專案自行開發的原始碼依根目錄的 [MIT License](LICENSE) 授權。

下列第三方軟體、套件、服務或使用者素材不因本專案採用 MIT License 而改變其原有授權。各項目仍適用原作者提供的授權條款；列於此處不代表其作者為本專案背書。

## 外部工具與整合

### HyperFrames

- 用途：產生與輸出 HTML-based 影片專案。
- 使用方式：程式透過 `npx hyperframes` 呼叫外部工具；HyperFrames 原始碼未包含在本 Repository 中。
- 授權：Apache License 2.0。
- 專案：https://github.com/heygen-com/hyperframes
- 授權：https://github.com/heygen-com/hyperframes/blob/main/LICENSE

### OpenCut Classic

- 用途：啟動另外安裝的 OpenCut Classic，並輸出可匯入的 handoff 素材與資料。
- 使用方式：OpenCut Classic 原始碼未包含在本 Repository 中。
- 授權：MIT License。
- 專案：https://github.com/opencut-app/opencut-classic
- 授權：https://github.com/opencut-app/opencut-classic/blob/main/LICENSE

### FFmpeg / FFprobe

- 用途：影片與音訊讀取、分析、轉碼、預覽及輸出。
- 使用方式：目前由使用者另外安裝，程式透過命令列呼叫；本 Repository 不提供 FFmpeg binary。
- 授權：FFmpeg 預設為 LGPL 2.1 or later；若建置時啟用 GPL 元件，該 binary 會適用 GPL 2.0 or later。實際條款以使用者安裝的 FFmpeg build 為準。
- 官方授權說明：https://ffmpeg.org/legal.html

若未來在安裝包或 Release 中直接附帶 FFmpeg binary，發布者必須另外確認該 build 的設定、完整授權義務、來源碼提供方式，以及其外部函式庫是否改變整體授權條件。

## Runtime-loaded library

### GSAP (GreenSock Animation Platform)

- 用途：產生的 HyperFrames HTML 使用 GSAP timeline 動畫。
- 使用方式：目前由產生的 HTML 經 jsDelivr CDN 載入 GSAP；GSAP 原始碼未包含在本 Repository 中。
- 授權：GreenSock Standard No Charge License；GSAP 不屬於本專案 MIT License 的授權範圍。
- 專案：https://github.com/greensock/GSAP
- 授權：https://gsap.com/standard-license/

使用或散布含 GSAP 的輸出內容時，仍應遵守 GreenSock 當時有效的授權條款。

## npm 套件

React WebUI 與開發流程使用 `web/package.json` 及 `web/package-lock.json` 中記錄的 npm 套件，包括：

- React / React DOM：MIT License
- Vite / `@vitejs/plugin-react`：MIT License
- TypeScript：Apache License 2.0
- Vitest、Testing Library、jsdom、DefinitelyTyped 型別套件及其傳遞依賴：依各套件自身授權

這些套件由 npm 安裝，`node_modules` 與 `web/dist` 不包含在 Repository 中。精確版本與依賴關係以 `web/package-lock.json` 為準。若發布編譯後的 WebUI、桌面程式或安裝包，發布者應保留適用的第三方著作權與授權聲明，並依實際發布內容產生完整的 dependency license report。

## 使用者提供的內容

使用者匯入或指定的下列內容不包含在本專案的 MIT License 中：

- 原始影片、照片與音訊
- BGM、音效及配音
- LUT、字型、圖示、圖片及其他素材
- AI 模型、模型輸出及雲端服務內容

使用者與發布者有責任確認這些內容可被使用、修改、輸出及散布，並依原授權完成署名、連結、來源說明或其他義務。

## 發布提醒

本文件是目前 Repository 直接整合與主要依賴的摘要，不取代任何第三方正式授權文字。若日後新增、內嵌、修改或隨程式散布第三方程式碼或 binary，應在發布前重新檢查並更新本文件。
