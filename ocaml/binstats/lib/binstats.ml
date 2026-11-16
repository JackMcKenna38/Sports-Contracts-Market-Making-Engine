type observation = { forecast : float; outcome : bool }

exception Invalid_forecast of float

let observation ~forecast ~outcome =
  if Float.is_nan forecast || forecast < 0. || forecast > 1. then
    raise (Invalid_forecast forecast);
  { forecast; outcome }

let indicator o = if o.outcome then 1. else 0.

let mean f = function
  | [] -> Float.nan
  | xs ->
      List.fold_left (fun acc x -> acc +. f x) 0. xs
      /. float_of_int (List.length xs)

let brier = mean (fun o -> (o.forecast -. indicator o) ** 2.)

let log_loss =
  let eps = 1e-12 in
  mean (fun o ->
      let p = Float.min (1. -. eps) (Float.max eps o.forecast) in
      -.log (if o.outcome then p else 1. -. p))

type bin = {
  lower : float;
  upper : float;
  count : int;
  mean_forecast : float;
  observed_rate : float;
}

let calibration ~bins observations =
  if bins < 1 then invalid_arg "Binstats.calibration: bins must be positive";
  let n = float_of_int bins in
  let counts = Array.make bins 0 in
  let forecasts = Array.make bins 0. in
  let hits = Array.make bins 0. in
  List.iter
    (fun o ->
      let i = min (bins - 1) (int_of_float (o.forecast *. n)) in
      counts.(i) <- counts.(i) + 1;
      forecasts.(i) <- forecasts.(i) +. o.forecast;
      hits.(i) <- hits.(i) +. indicator o)
    observations;
  List.init bins (fun i ->
      let count = counts.(i) in
      let ratio total =
        if count = 0 then Float.nan else total /. float_of_int count
      in
      {
        lower = float_of_int i /. n;
        upper = float_of_int (i + 1) /. n;
        count;
        mean_forecast = ratio forecasts.(i);
        observed_rate = ratio hits.(i);
      })

let expected_calibration_error bins =
  let total = List.fold_left (fun acc b -> acc + b.count) 0 bins in
  if total = 0 then Float.nan
  else
    List.fold_left
      (fun acc b ->
        if b.count = 0 then acc
        else
          acc
          +. float_of_int b.count
             *. Float.abs (b.observed_rate -. b.mean_forecast))
      0. bins
    /. float_of_int total

type side = Yes | No

type kelly = { side : side; fraction : float; edge : float }

let kelly ~probability ~price =
  if not (price > 0. && price < 1.) then
    invalid_arg "Binstats.kelly: price must be strictly between 0 and 1";
  if not (probability >= 0. && probability <= 1.) then
    invalid_arg "Binstats.kelly: probability must be within [0, 1]";
  if probability > price then
    Some
      {
        side = Yes;
        fraction = (probability -. price) /. (1. -. price);
        edge = (probability -. price) /. price;
      }
  else if probability < price then
    Some
      {
        side = No;
        fraction = (price -. probability) /. price;
        edge = (price -. probability) /. (1. -. price);
      }
  else None

let split_fields line = List.map String.trim (String.split_on_char ',' line)

let index_of name fields =
  let rec go i = function
    | [] -> None
    | x :: _ when String.lowercase_ascii x = name -> Some i
    | _ :: rest -> go (i + 1) rest
  in
  go 0 fields

let parse_csv lines =
  let lines =
    List.filter (fun l -> String.trim l <> "") lines
    |> List.map (fun l ->
           let n = String.length l in
           if n > 0 && l.[n - 1] = '\r' then String.sub l 0 (n - 1) else l)
  in
  match lines with
  | [] -> Error "empty input: expected a header naming fair and outcome"
  | header :: rows -> (
      let fields = split_fields header in
      match (index_of "fair" fields, index_of "outcome" fields) with
      | None, _ -> Error "header has no 'fair' column"
      | _, None -> Error "header has no 'outcome' column"
      | Some fair_i, Some outcome_i ->
          let parse_row number line =
            let cells = Array.of_list (split_fields line) in
            let cell i =
              if i < Array.length cells then Ok cells.(i)
              else Error (Printf.sprintf "line %d: too few columns" number)
            in
            match (cell fair_i, cell outcome_i) with
            | Error e, _ | _, Error e -> Error e
            | Ok fair, Ok outcome -> (
                match (float_of_string_opt fair, outcome) with
                | None, _ ->
                    Error
                      (Printf.sprintf "line %d: fair %S is not a number" number
                         fair)
                | Some forecast, ("0" | "1") -> (
                    try Ok (observation ~forecast ~outcome:(outcome = "1"))
                    with Invalid_forecast f ->
                      Error
                        (Printf.sprintf "line %d: fair %g is outside [0, 1]"
                           number f))
                | Some _, other ->
                    Error
                      (Printf.sprintf "line %d: outcome %S is not 0 or 1"
                         number other))
          in
          let rec go number acc = function
            | [] -> Ok (List.rev acc)
            | line :: rest -> (
                match parse_row number line with
                | Ok o -> go (number + 1) (o :: acc) rest
                | Error e -> Error e)
          in
          go 2 [] rows)
