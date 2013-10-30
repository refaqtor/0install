(* Copyright (C) 2013, Thomas Leonard
 * See the README file for details, or visit http://0install.net.
 *)

(** Generating the .manifest files *)

type alg = string
type digest = string * string

val parse_digest : string -> digest
val format_digest : digest -> string

val algorithm_names : string list

(** [generate_manifest system alg dir] scans [dir] and return the contents of the generated manifest.
 * If the directory contains a .manifest file at the top level, it is ignored. *)
val generate_manifest : Support.Common.system -> alg -> Support.Common.filepath -> string

(** Generate the final overall hash value of the manifest. *)
val hash_manifest : alg -> string -> string

(** Writes a .manifest file into 'dir', and returns the digest.
    You should call Stores.fixup_permissions before this to ensure that the permissions are correct.
    On exit, dir itself has mode 555. Subdirectories are not changed.
    @return the value part of the digest of the manifest. *)
val add_manifest_file : Support.Common.system -> alg -> Support.Common.filepath -> string

(** Ensure that directory 'dir' generates the given digest.
    @param digest the required digest (usually this is just [Filename.basename dir])
    For a non-error return:
    - The calculated digest of the contents must match [digest].
    - If there is a .manifest file, then its digest must also match. *)
val verify : Support.Common.system -> digest:digest -> Support.Common.filepath -> unit