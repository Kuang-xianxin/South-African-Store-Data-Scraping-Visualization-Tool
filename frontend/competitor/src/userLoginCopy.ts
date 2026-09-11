// The employee entry must remain public even when an administrator uses localhost/LAN.
export const ERP_PUBLIC_LOGIN_URL = "https://119.91.117.232/";

export function formatUserLogin(username: string, password: string): string {
  if (!username || !password) throw new Error("请先填写该账号的密码");
  return [
    "昂古古科技有限公司ERP",
    `系统地址：${ERP_PUBLIC_LOGIN_URL}`,
    `账号：${username}`,
    `密码：${password}`,
  ].join("\n");
}

export async function copyUserLogin(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // LAN HTTP and clipboard permission failures use the local copy fallback.
    }
  }
  const previousFocus = document.activeElement;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.cssText = "position:fixed;left:0;top:0;opacity:0;font-size:16px";
  // A modal makes the rest of the document inert, so keep its fallback inside it.
  (document.querySelector("dialog[open]") ?? document.body).appendChild(textarea);
  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    if (!document.execCommand("copy")) throw new Error("复制失败，请重试或手动复制下方内容");
  } finally {
    textarea.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus({ preventScroll: true });
  }
}
