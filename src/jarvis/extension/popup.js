const status = document.getElementById("status");
const show = async () => { status.textContent = (await chrome.storage.local.get("jarvisStatus")).jarvisStatus || "Not paired yet"; };
document.getElementById("save").addEventListener("click", async () => {
  const key = document.getElementById("key").value.trim();
  if (key) await chrome.storage.local.set({ jarvisKey: key });
  setTimeout(show, 1500);
});
chrome.storage.onChanged.addListener(show);
show();
