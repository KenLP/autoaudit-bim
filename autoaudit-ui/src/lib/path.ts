/** Returns the last path segment for a Windows or POSIX path string.
 *  Used to display rule/profile file names without the (often Windows,
 *  often long) full path — callers should still put the full path in a
 *  `title` tooltip so it isn't lost.
 */
export function basename(path: string): string {
  if (!path) return path;
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts.length > 0 ? parts[parts.length - 1] : path;
}
