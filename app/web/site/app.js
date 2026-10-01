// 落地页交互：拉取服务端元信息，动态显示版本与下载可用性。
(function () {
  "use strict";

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) el.textContent = text;
  }

  function applyInfo(info) {
    if (!info) return;
    if (info.version) {
      setText("version-badge", "v" + info.version);
    }

    var ready = !!info.download_ready;
    var note = document.getElementById("download-state");
    var btns = [
      document.getElementById("download-btn"),
      document.getElementById("download-btn-2"),
    ];

    if (ready) {
      if (note) note.textContent = "安装包已就绪，点击即可下载。";
      btns.forEach(function (b) {
        if (b) b.classList.remove("disabled");
      });
    } else {
      if (note) note.textContent = "安装包尚未上传，敬请期待。";
      btns.forEach(function (b) {
        if (b) b.classList.add("disabled");
      });
    }
  }

  fetch("/api/site/info")
    .then(function (r) {
      return r.ok ? r.json() : null;
    })
    .then(applyInfo)
    .catch(function () {
      // 接口不可用时：保持按钮可用，让下载请求自己返回结果
      setText("download-state", "点击下载（如未上传会提示）。");
    });
})();
