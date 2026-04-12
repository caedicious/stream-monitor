// Open links in new tabs (extension pages can't navigate directly)
document.getElementById("link-twitch").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "https://www.twitch.tv/CaedVT" });
});

document.getElementById("link-github").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "https://github.com/caedicious/stream-monitor" });
});

document.getElementById("link-kofi").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "https://ko-fi.com/caedvt" });
});
