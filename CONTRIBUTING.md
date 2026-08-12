# Contributing to OpenTransLive

歡迎貢獻！送出 Pull Request 前請先讀完本文件。
Thanks for contributing. Please read this document before opening a Pull Request.

語言：繁體中文 / English（本文件雙語，內容等效；若有歧義以英文版為準）。

---

## 1. 開發流程 / Workflow

1. Fork 本專案 / Fork the repo
2. 建立功能分支 / Create a feature branch (`git checkout -b feature/amazing-feature`)
3. 提交變更，**帶上 `-s`** / Commit **with `-s`** (`git commit -s -m 'Add amazing feature'`)
4. 推送分支 / Push (`git push origin feature/amazing-feature`)
5. 開啟 Pull Request

新增的原始檔請沿用既有的授權標頭 / New source files must carry the standard license header:

```
This file is part of g0v/OpenTransLive.
Copyright (c) 2025 Sean Gau <rrtw0627@gmail.com>
Licensed under the GNU AGPL v3.0
See LICENSE for details.
```

---

## 2. 貢獻者授權同意書（CLA）/ Contributor License Agreement

OpenTransLive 以 GNU AGPL v3.0 對外釋出，同時保留提供商業授權的可能。為了讓專案能持續維持這個模式，**所有貢獻都必須同意以下條款**。

OpenTransLive is released under the GNU AGPL v3.0 while reserving the ability to
offer commercial licenses. To keep that possible, **all contributions must be
made under the following terms.**

本文中「你」指送出貢獻的個人；「貢獻」指你提交到本專案的任何原始碼、文件、設定或其他素材；「專案擁有者」指 Sean Gau <rrtw0627@gmail.com>。

"You" means the individual submitting a Contribution. "Contribution" means any
source code, documentation, configuration, or other material you submit to this
project. "Project Owner" means Sean Gau <rrtw0627@gmail.com>.

### 2.1 著作權授權 / Copyright license

你保留你的貢獻的著作權。同時，你授予專案擁有者一份**永久、全球、非專屬、免權利金、不可撤銷**的授權，允許其重製、改作、公開展示、公開演出、再授權及散布你的貢獻與其衍生作品，**並得以任何授權條款（包含專屬的商業授權）再授權**。

You retain copyright in your Contribution. You also grant the Project Owner a
**perpetual, worldwide, non-exclusive, royalty-free, irrevocable** license to
reproduce, prepare derivative works of, publicly display, publicly perform,
sublicense, and distribute your Contribution and such derivative works, **under
any license terms, including proprietary commercial licenses.**

### 2.2 專利授權 / Patent license

你授予專案擁有者及本專案的使用者一份永久、全球、非專屬、免權利金、不可撤銷的專利授權，涵蓋你所擁有、且會因你的貢獻（單獨或與本專案結合）而被實施的專利請求項。

You grant the Project Owner and users of the project a perpetual, worldwide,
non-exclusive, royalty-free, irrevocable patent license covering patent claims
you own that are necessarily infringed by your Contribution alone or in
combination with the project.

### 2.3 你的聲明 / Your representations

你聲明並保證：

You represent and warrant that:

- 貢獻是你原創的，或你有權以本條款提交 / the Contribution is your original work, or you have the right to submit it under these terms;
- 貢獻不含你不得授權的第三方程式碼；若含有第三方素材，你已在 PR 中明確標示其來源與授權 / it contains no third-party material you are not entitled to license; any third-party material is clearly identified in the PR along with its source and license;
- 若你受僱於人且貢獻與工作相關，你已取得雇主同意，或雇主已放棄相關權利 / if you are employed and the Contribution relates to your work, you have your employer's permission or your employer has waived its rights.

貢獻依「現狀」提供，不附任何明示或默示擔保。
Contributions are provided "AS IS", without warranty of any kind.

### 2.4 表示同意的方式 / How to signify agreement

在每個 commit 加上 `Signed-off-by` 行即代表你已閱讀並同意本 CLA：

Adding a `Signed-off-by` line to each commit signifies that you have read and
agree to this CLA:

```bash
git commit -s -m "your message"
```

這會產生 / This produces:

```
Signed-off-by: Your Name <your.email@example.com>
```

請使用你的真實姓名與可聯絡的 email。缺少 sign-off 的 PR 無法合併。

Use your real name and a reachable email address. PRs without sign-off cannot be
merged.

---

## 3. 商標 / Trademark

`OpenTransLive` 名稱與相關識別標誌不在 AGPL 授權範圍內（AGPL 第 7 條不授予商標權）。貢獻本專案不會取得任何商標授權；fork 或衍生作品請使用不同的名稱與品牌識別。

The `OpenTransLive` name and logos are not covered by the AGPL (section 7 grants
no trademark rights). Contributing grants you no trademark license; forks and
derivative works must use a different name and branding.

---

## 4. 問題回報 / Reporting issues

一般問題請開 [issue](https://github.com/g0v/OpenTransLive/issues)。
安全性弱點請勿公開揭露，直接寄信給 Sean Gau <rrtw0627@gmail.com>。

For general issues, open an [issue](https://github.com/g0v/OpenTransLive/issues).
For security vulnerabilities, do not disclose publicly — email Sean Gau
<rrtw0627@gmail.com> directly.
