import Lake

open Lake System

private def usage : String :=
  "usage: CacheArtifacts CACHE_DIR INPUT.tsv OUTPUT.tsv\n" ++
  "INPUT.tsv contains one absolute path, a tab, and the Lake artifact extension per line."

private def parseLine (line : String) : IO (FilePath × String) := do
  match line.splitOn "\t" with
  | [path, ext] =>
    if path.isEmpty || ext.isEmpty then
      throw <| IO.userError s!"invalid empty manifest field: {line}"
    return (path, ext)
  | _ => throw <| IO.userError s!"invalid manifest line: {line}"

private def cacheOne (cache : Cache) (output : FilePath) (line : String) : IO Unit := do
  let (path, ext) ← parseLine line
  let artifact ← cache.saveArtifact path ext (useLocalFile := true)
  IO.FS.withFile output .append fun handle => do
    handle.putStrLn s!"{path}\t{artifact.descr.relPath}"
    handle.flush

def main (args : List String) : IO UInt32 := do
  match args with
  | [cacheDir, input, output] =>
    let input : FilePath := input
    let output : FilePath := output
    if ← output.pathExists then
      IO.FS.removeFile output
    let lines := (← IO.FS.readFile input).splitOn "\n" |>.filter (not ∘ String.isEmpty)
    for line in lines do
      cacheOne ⟨cacheDir⟩ output line
    return 0
  | _ =>
    IO.eprintln usage
    return 2
