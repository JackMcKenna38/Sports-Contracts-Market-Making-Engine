let usage = "binstats [--bins N] FILE.csv\n\nReads a CSV with 'fair' and 'outcome' columns ('-' for stdin) and prints the\ncalibration of the fair values as a Markdown table."

let read_lines channel =
  let rec go acc =
    match input_line channel with
    | line -> go (line :: acc)
    | exception End_of_file -> List.rev acc
  in
  go []

let cell x = if Float.is_nan x then "-" else Printf.sprintf "%.4f" x

let print_report bins observations =
  let curve = Binstats.calibration ~bins observations in
  print_endline "| forecast | n | mean forecast | observed rate |";
  print_endline "| --- | ---: | ---: | ---: |";
  List.iter
    (fun (b : Binstats.bin) ->
      Printf.printf "| %.2f-%.2f | %d | %s | %s |\n" b.lower b.upper b.count
        (cell b.mean_forecast) (cell b.observed_rate))
    curve;
  Printf.printf "\nobservations: %d\n" (List.length observations);
  Printf.printf "brier: %s\n" (cell (Binstats.brier observations));
  Printf.printf "log loss: %s\n" (cell (Binstats.log_loss observations));
  Printf.printf "expected calibration error: %s\n"
    (cell (Binstats.expected_calibration_error curve))

let () =
  let bins = ref 10 in
  let path = ref None in
  let spec =
    [ ("--bins", Arg.Set_int bins, "N number of equal-width buckets (default 10)") ]
  in
  Arg.parse spec (fun p -> path := Some p) usage;
  let fail message =
    prerr_endline ("binstats: " ^ message);
    exit 1
  in
  if !bins < 1 then fail "--bins must be positive";
  let lines =
    match !path with
    | None ->
        prerr_endline usage;
        exit 2
    | Some "-" -> read_lines stdin
    | Some file -> (
        match open_in file with
        | channel ->
            Fun.protect
              ~finally:(fun () -> close_in channel)
              (fun () -> read_lines channel)
        | exception Sys_error message -> fail message)
  in
  match Binstats.parse_csv lines with
  | Error message -> fail message
  | Ok [] -> fail "no observations"
  | Ok observations -> print_report !bins observations
