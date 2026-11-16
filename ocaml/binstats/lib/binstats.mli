(** Statistics for binary contracts that settle at 0 or 1.

    A forecast is a probability in [[0, 1]] that the contract settles at 1; an
    observation pairs a forecast with the realised outcome. *)

type observation = { forecast : float; outcome : bool }

exception Invalid_forecast of float

val observation : forecast:float -> outcome:bool -> observation
(** @raise Invalid_forecast if [forecast] is outside [[0, 1]] or not finite. *)

val brier : observation list -> float
(** Mean squared error of the forecasts. [nan] for an empty list. *)

val log_loss : observation list -> float
(** Mean negative log-likelihood, with forecasts clamped away from 0 and 1 by
    [1e-12]. [nan] for an empty list. *)

type bin = {
  lower : float;
  upper : float;
  count : int;
  mean_forecast : float;
  observed_rate : float;
}
(** One calibration bucket over forecasts in [[lower, upper)]; the last bucket
    also includes 1. [mean_forecast] and [observed_rate] are [nan] when
    [count] is 0. *)

val calibration : bins:int -> observation list -> bin list
(** Equal-width calibration curve.
    @raise Invalid_argument if [bins < 1]. *)

val expected_calibration_error : bin list -> float
(** Count-weighted mean of [|observed_rate - mean_forecast|] over non-empty
    buckets. [nan] when every bucket is empty. *)

type side = Yes | No

type kelly = { side : side; fraction : float; edge : float }
(** [fraction] is the share of bankroll to stake; [edge] is the expected profit
    per unit staked. *)

val kelly : probability:float -> price:float -> kelly option
(** Full-Kelly stake for a contract trading at [price] (the cost of YES; NO
    costs [1 - price]) when it settles at 1 with [probability]. [None] when
    there is no edge on either side.
    @raise Invalid_argument unless [0 < price < 1] and [0 <= probability <= 1]. *)

val parse_csv : string list -> (observation list, string) result
(** Parse CSV lines whose header names a [fair] and an [outcome] column. Other
    columns are ignored; [outcome] must be [0] or [1]. Blank lines are skipped. *)
