import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { Button } from "../ui";

/** 明暗主题切换：切换 <html>.dark 并持久化到 localStorage（首次跟随系统）。 */
export function ThemeToggle() {
  const [dark, setDark] = useState(() =>
    document.documentElement.classList.contains("dark"),
  );

  const toggle = () => {
    const next = !dark;
    setDark(next);
    document.documentElement.classList.toggle("dark", next);
    localStorage.setItem("theme", next ? "dark" : "light");
  };

  return (
    <Button size="icon" variant="ghost" onClick={toggle} aria-label="切换主题">
      {dark ? <Sun /> : <Moon />}
    </Button>
  );
}