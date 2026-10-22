# MiSans 网页字体

来源：小米官方 `https://hyperos.mi.com/font-download/MiSans.zip`。

这里保留官方 ZIP 中原始 WOFF2 文件，没有转换、裁剪、改名字体家族或修改字形。字体版本为 4.009。CSS 将 Regular、Medium、Semibold 分别映射到网页排版的 400、500、600 字重。

应用界面使用 MiSans。许可见同目录 `MiSans-font-license.pdf`；发布应用时一起保留许可与字体版权。字体不作为独立产品分发，不安装到用户的系统字体目录。

字体请求使用 `?v=4.009` 的版本标识，并由应用以白名单路径提供缓存响应。未来替换字体资源时应同时更新版本 URL，不沿用旧的不可变缓存地址。HTML、脚本、样式和业务接口仍保持原有缓存策略。
