open Binstats

let failures = ref 0

let check name condition =
  if not condition then (
    incr failures;
    Printf.eprintf "FAIL %s\n" name)

let close ?(eps = 1e-12) a b = Float.abs (a -. b) <= eps
let obs forecast outcome = observation ~forecast ~outcome

let raises f =
  match f () with _ -> false | exception (Invalid_argument _ | Invalid_forecast _) -> true

let test_brier () =
  check "brier empty is nan" (Float.is_nan (brier []));
  check "brier perfect" (close (brier [ obs 1. true; obs 0. false ]) 0.);
  check "brier worst" (close (brier [ obs 0. true; obs 1. false ]) 1.);
  check "brier coin flip" (close (brier [ obs 0.5 true; obs 0.5 false ]) 0.25);
  (* (0.3^2 + 0.6^2 + 0.1^2) / 3 = 0.46 / 3 *)
  check "brier worked example"
    (close (brier [ obs 0.7 true; obs 0.6 false; obs 0.9 true ]) (0.46 /. 3.))

let test_log_loss () =
  check "log loss coin flip" (close (log_loss [ obs 0.5 true ]) (log 2.));
  check "log loss is finite at the boundary"
    (Float.is_finite (log_loss [ obs 0. true ]));
  check "log loss empty is nan" (Float.is_nan (log_loss []))

let test_calibration () =
  let curve =
    calibration ~bins:4
      [ obs 0.1 false; obs 0.2 true; obs 0.6 true; obs 1.0 true; obs 0.75 false ]
  in
  check "four bins" (List.length curve = 4);
  let b0 = List.nth curve 0 and b1 = List.nth curve 1 in
  let b2 = List.nth curve 2 and b3 = List.nth curve 3 in
  check "bin bounds" (close b1.lower 0.25 && close b1.upper 0.5);
  check "bin 0 count" (b0.count = 2);
  check "bin 0 mean forecast" (close b0.mean_forecast 0.15);
  check "bin 0 observed" (close b0.observed_rate 0.5);
  check "empty bin is nan"
    (b1.count = 0 && Float.is_nan b1.mean_forecast && Float.is_nan b1.observed_rate);
  check "bin 2" (b2.count = 1 && close b2.observed_rate 1.);
  check "forecast of 1 lands in the last bin, boundary goes up"
    (b3.count = 2 && close b3.mean_forecast 0.875 && close b3.observed_rate 0.5);
  check "counts sum to n"
    (List.fold_left (fun acc b -> acc + b.count) 0 curve = 5);
  (* (2*|0.5-0.15| + 1*|1-0.6| + 2*|0.5-0.875|) / 5 = 1.85 / 5 *)
  check "ece" (close (expected_calibration_error curve) 0.37);
  check "ece of nothing is nan"
    (Float.is_nan (expected_calibration_error (calibration ~bins:3 [])));
  check "bins must be positive" (raises (fun () -> calibration ~bins:0 []))

let test_kelly () =
  (match kelly ~probability:0.6 ~price:0.5 with
  | Some { side = Yes; fraction; edge } ->
      check "kelly yes fraction" (close fraction 0.2);
      check "kelly yes edge" (close edge 0.2)
  | _ -> check "kelly yes side" false);
  (match kelly ~probability:0.3 ~price:0.4 with
  | Some { side = No; fraction; edge } ->
      check "kelly no fraction" (close fraction 0.25);
      check "kelly no edge" (close edge (0.1 /. 0.6))
  | _ -> check "kelly no side" false);
  check "no edge, no bet" (kelly ~probability:0.5 ~price:0.5 = None);
  (match kelly ~probability:1. ~price:0.9 with
  | Some { fraction; _ } -> check "certain win stakes everything" (close fraction 1.)
  | None -> check "certain win" false);
  check "price of 0 rejected" (raises (fun () -> kelly ~probability:0.5 ~price:0.));
  check "price of 1 rejected" (raises (fun () -> kelly ~probability:0.5 ~price:1.));
  check "probability above 1 rejected"
    (raises (fun () -> kelly ~probability:1.2 ~price:0.5))

let test_observation () =
  check "forecast above 1 rejected" (raises (fun () -> obs 1.01 true));
  check "negative forecast rejected" (raises (fun () -> obs (-0.1) true));
  check "nan forecast rejected" (raises (fun () -> obs Float.nan true))

let test_parse_csv () =
  (match parse_csv [ "x, Fair ,outcome"; "a,0.25,0"; ""; "b,0.75,1\r" ] with
  | Ok [ a; b ] ->
      check "csv rows"
        (close a.forecast 0.25 && (not a.outcome) && close b.forecast 0.75 && b.outcome)
  | _ -> check "csv parses" false);
  let is_error = function Error _ -> true | Ok _ -> false in
  check "csv empty" (is_error (parse_csv []));
  check "csv missing column" (is_error (parse_csv [ "fair,result"; "0.5,1" ]));
  check "csv bad outcome" (is_error (parse_csv [ "fair,outcome"; "0.5,yes" ]));
  check "csv bad number" (is_error (parse_csv [ "fair,outcome"; "half,1" ]));
  check "csv out of range" (is_error (parse_csv [ "fair,outcome"; "1.5,1" ]));
  check "csv short row" (is_error (parse_csv [ "a,fair,outcome"; "0.5" ]));
  check "csv header only" (parse_csv [ "fair,outcome" ] = Ok [])

let () =
  test_brier ();
  test_log_loss ();
  test_calibration ();
  test_kelly ();
  test_observation ();
  test_parse_csv ();
  if !failures > 0 then (
    Printf.eprintf "%d check(s) failed\n" !failures;
    exit 1)
  else print_endline "binstats: all checks passed"
