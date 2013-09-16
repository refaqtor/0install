(* Copyright (C) 2013, Thomas Leonard
 * See the README file for details, or visit http://0install.net.
 *)

open Ocamlbuild_plugin;;

let on_windows = Sys.os_type = "Win32"

let print_info f =
  Format.fprintf Format.std_formatter
    "@[<hv 2>Tags for file %s:@ %a@]@." f
    Tags.print (tags_of_pathname f)

let () =
  dispatch (function
  | After_rules ->
    pdep ["link"] "linkdep_win" (fun param -> if on_windows then [param] else []);

    (* We use mypp rather than camlp4of because if you pass -pp and -ppopt to ocamlfind
       then it just ignores the ppopt. So, we need to write the -pp option ourselves. *)

    let pp_portable = "camlp4of" in
    let pp_native =
      if on_windows then
        "camlp4of -DWINDOWS"
      else
        "camlp4of"
    in
    flag ["native";"ocaml";"compile";"mypp"] (S [A"-pp"; A pp_native]);
    flag ["byte";"ocaml";"compile";"mypp"] (S [A"-pp"; A pp_portable]);

    flag ["ocaml";"ocamldep";"mypp"] (S [A"-pp"; A "camlp4of"]);

    (* Enable most warnings *)
    flag ["compile"; "ocaml"] (S [A"-w"; A"A-4"]);

    pflag [] "dllib" (fun x -> (S [A"-dllib"; A x]));

    (* (<*.ml> or <support/*.ml> or <zeroinstall/*.ml> or <cmd/*.ml>): bisect, syntax(bisect_pp) *)

    (* Code coverage with bisect *)
    let coverage =
      try Sys.getenv "OCAML_COVERAGE" = "true"
      with Not_found -> false in

    if coverage then (
      flag ["compile"; "ocaml"] (S [A"-package"; A"bisect"; A"-syntax"; A"camlp4o"]);
      flag ["link"] (S [A"-package"; A"bisect"]);
    );
  | _ -> ()
  )

