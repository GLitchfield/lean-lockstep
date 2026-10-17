import Lean

open Lean

def main (args : List String) : IO UInt32 := do
  let [parent] := args | throw <| IO.userError "expected parent declaration"
  initSearchPath (← findSysroot)
  let env ← importModules #[{module := `Candidate}] {} 0
  let name := parent.toName
  let some ci := env.find? name | throw <| IO.userError "parent declaration missing"
  let (_, state) := ((Lean.CollectAxioms.collect name).run env).run {}
  let hash := String.ofList (Nat.toDigits 16 (mixHash ci.type.hash (hash ci.levelParams)).toNat)
  let result := Json.mkObj [
    ("parent", toJson parent),
    ("type_hash", toJson hash),
    ("axioms", toJson (state.axioms.map Name.toString))]
  (← IO.getStdout).putStrLn result.compress
  return 0
