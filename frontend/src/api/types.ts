/** Shapes returned by the backend. Kept narrow: only what the UI reads. */

export type PipelineStatus =
  | "uploaded"
  | "extracting"
  | "extract_failed"
  | "extracted"
  | "normalized"
  | "reconciled"
  | "resolving"
  | "needs_review"
  | "complete";

export type ReconciliationStatus =
  | "not_attempted"
  | "balanced"
  | "suspect"
  | "unreconcilable";

export type ResolutionSource =
  | "correction_store"
  | "correction_global"
  | "llm"
  | "unresolved"
  | "nonfood";

export type LineItemKind =
  | "product"
  | "discount"
  | "fee"
  | "tax"
  | "subtotal"
  | "total"
  | "unknown";

export interface ReceiptSummary {
  id: number;
  status: PipelineStatus;
  reconciliation_status: ReconciliationStatus;
  store_id: number | null;
  store_name: string | null;
  purchased_at: string | null;
  total_cents: number | null;
  currency: string;
  uploaded_at: string;
}

export interface ReceiptDetail extends ReceiptSummary {
  subtotal_cents: number | null;
  tax_cents: number | null;
  reconciliation_delta_cents: number | null;
  reconciliation_report: Record<string, unknown> | null;
  extraction_model: string | null;
  extracted_at: string | null;
  duplicate_of_receipt_id: number | null;
  image_bytes: number;
  image_sha256: string;
}

export interface UploadResponse {
  receipt: ReceiptDetail;
  /** false when this file had already been uploaded — not a second receipt. */
  created: boolean;
  width: number;
  height: number;
  image_format: string;
}

export interface LineItem {
  id: number;
  line_index: number;
  raw_text: string;
  normalized_text: string;
  kind: LineItemKind;
  price_cents: number;
  quantity: string | null;
  unit: string | null;
  grams_as_purchased: string | null;
  grams_edible: string | null;
  grams_basis: string;
  food_id: number | null;
  food_name: string | null;
  resolution_source: ResolutionSource;
  confidence: number | null;
}

export interface LineItemList {
  receipt_id: number;
  items: LineItem[];
  resolved: number;
  total: number;
  coverage: number;
}

export interface ResolutionResult {
  receipt_id: number;
  status: PipelineStatus;
  by_source: Record<string, number>;
  coverage: number;
  unresolved: string[];
  cost_usd: string;
  latency_ms: number | null;
}

export interface Coverage {
  lines_total: number;
  lines_resolved: number;
  lines_with_nutrition: number;
  spend_share: number;
  weight_share: number;
  grams_total: string;
  grams_with_nutrition: string;
  unresolved_lines: number;
  lines_without_nutrition: number;
  lines_without_weight: number;
  is_partial: boolean;
}

export interface BasketSummary {
  receipt_ids: number[];
  starts_on: string | null;
  ends_on: string | null;
  currency: string;
  total_spend_cents: number;
  nutrients: Record<string, string>;
  units: Record<string, string>;
  coverage: Coverage;
  headline: string;
}

export interface NutrientCostRow {
  food_id: number;
  canonical_name: string;
  observations: number;
  median_price_cents_per_100g: string;
  nutrient_per_100g: string;
  cost_cents_per_unit: string;
  from_receipt_weights: number;
}

export interface NutrientCost {
  nutrient: string;
  label: string;
  unit: string;
  items: NutrientCostRow[];
}

export interface BudgetStatus {
  month: string;
  limit_usd: string | null;
  spent_usd: string;
  remaining_usd: string | null;
  is_exhausted: boolean;
  call_count: number;
}

export interface FoodSummary {
  id: number;
  canonical_name: string;
  category: string;
  fdc_id: number | null;
  nutrient_count: number;
  has_nutrition: boolean;
}

export interface CorrectionBody {
  food_id?: number | null;
  is_nonfood?: boolean;
  grams_basis?: "per_package" | "per_unit_estimate" | "unknown";
  grams_value?: string | null;
  global_scope?: boolean;
}

export interface FoodList {
  items: FoodSummary[];
  total: number;
  without_nutrition: number;
}
