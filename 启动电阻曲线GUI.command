#!/bin/zsh

cd "$(dirname "$0")" || exit 1

if [ ! -x "./ResistanceGUI" ] || [ "ResistanceGUI.swift" -nt "./ResistanceGUI" ]; then
  echo "正在编译实时电阻曲线 GUI..."
  swiftc ResistanceGUI.swift -o ResistanceGUI
  if [ $? -ne 0 ]; then
    echo ""
    echo "编译失败。请确认已安装 Xcode Command Line Tools。"
    echo "可以在终端运行: xcode-select --install"
    echo ""
    read "?按回车键退出..."
    exit 1
  fi
fi

echo "正在启动实时电阻曲线 GUI..."
./ResistanceGUI
