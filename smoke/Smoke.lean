import Polyglot.Core.Lir
import Polyglot.Typescript.Ast
import Syntax.Prec

-- Smoke: forces the imported atlas oleans (fetched from the GitHub release asset
-- via //lean:atlas) to actually load — version check + symbol table. If the
-- archive held dangling symlinks or a toolchain-mismatched olean, this fails.
def smokeAtlasLoads : Nat := 0

-- Gate that the generic Syntax precedence kernel stays in the atlas — added in
-- atlas-v0.3.1 because rules_texlive's Pascal-H parser consumes it. Reference
-- real symbols (the AST type + the precedence parser) so a future atlas cut that
-- drops Syntax/{Expr,Prec} fails here instead of silently in a consumer.
example : Type := Syntax.Expr
#check @Syntax.Grammar.parse
