import { lstat } from "node:fs/promises";
import path from "node:path";

/** Never follow a repository symlink while selecting public site inputs. */
export async function safeInput(root, relative) {
  const components = relative.split("/");
  if (path.isAbsolute(relative) || components.some((part) => !part || part === "." || part === "..")) {
    throw new Error("Site input must be a relative repository path.");
  }
  let current = root;
  for (const [index, component] of components.entries()) {
    current = path.join(current, component);
    const info = await lstat(current);
    if (info.isSymbolicLink()) throw new Error("Site inputs must not contain symlinks.");
    if (index === components.length - 1 && !info.isFile()) throw new Error("Site input must be a regular file.");
    if (index < components.length - 1 && !info.isDirectory()) throw new Error("Site input parent must be a directory.");
  }
  return current;
}
