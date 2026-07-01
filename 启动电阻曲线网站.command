#!/bin/zsh

cd "$(dirname "$0")" || exit 1

PORT="${PORT:-5173}"
URL="http://localhost:${PORT}"

echo "正在启动实时电阻曲线网站..."
echo "项目目录: $(pwd)"
echo "访问地址: ${URL}"
echo ""
echo "请保持这个窗口打开。关闭窗口后，网站也会停止。"
echo ""

(sleep 1
if open -Ra "Google Chrome"; then
  open -a "Google Chrome" "${URL}"
elif open -Ra "Microsoft Edge"; then
  open -a "Microsoft Edge" "${URL}"
else
  open "${URL}"
fi) >/dev/null 2>&1 &
python3 -m http.server "${PORT}"
