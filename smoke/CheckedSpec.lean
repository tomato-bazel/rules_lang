import Polyglot.Core.Lir

namespace CheckedSpec
open Polyglot.Core

def constant : Lir.Module := {
  decls := [.valDecl "answer" true (some (.prim .int)) (.int 42)]
}

def wrongTarget : Lir.Module := {
  decls := [.raw (.other "fixture-other") "rejected fixture"]
}

def legacyRender (_ : Lir.Module) : String := "legacy fixture\n"
end CheckedSpec
