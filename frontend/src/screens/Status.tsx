/**
 * How the system itself is doing.
 *
 * Everything here is maintenance rather than daily use, which is why it sits
 * behind one link instead of taking a tab: spend against the ceiling, and the
 * foods still waiting on nutrition data.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BudgetStatus, FoodList, FoodSummary } from "../api/types";
import { NutritionFix } from "../components/NutritionFix";
import { Card, ErrorNote, Skeleton } from "../components/ui";

export function Status() {
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [foods, setFoods] = useState<FoodList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fixing, setFixing] = useState<FoodSummary | null>(null);
  const [fixed, setFixed] = useState<string | null>(null);

  function load() {
    Promise.all([api.budget(), api.foods(true)])
      .then(([budgetStatus, foodList]) => {
        setBudget(budgetStatus);
        setFoods(foodList);
      })
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.message : "Couldn't load status."),
      );
  }

  useEffect(load, []);

  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!budget || !foods) return <Skeleton rows={3} />;

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-title font-semibold tracking-[-0.01em]">Status</h1>

      <Card accent={budget.is_exhausted ? "fail" : undefined}>
        <p className="text-label font-medium text-ink-2">Spend this month</p>
        <p className="text-display font-semibold tracking-[-0.01em]">
          ${Number(budget.spent_usd).toFixed(2)}
        </p>
        <p className="text-label text-ink-2">
          {budget.limit_usd
            ? `of $${Number(budget.limit_usd).toFixed(2)} · ${budget.call_count} model calls`
            : `no ceiling set · ${budget.call_count} model calls`}
        </p>
        {budget.is_exhausted && (
          <p className="text-body">
            The ceiling is reached. New receipts are stored but not processed
            until next month, or until the limit is raised.
          </p>
        )}
      </Card>

      {fixed && (
        <Card accent="resolved">
          <p className="text-body">
            Attached {fixed}. Every receipt line for this food now counts toward
            your totals.
          </p>
        </Card>
      )}

      {fixing ? (
        <NutritionFix
          food={fixing}
          onCancel={() => setFixing(null)}
          onFixed={(description) => {
            setFixing(null);
            setFixed(description);
            load();
          }}
        />
      ) : (
        <Card accent={foods.without_nutrition > 0 ? "attention" : undefined}>
          <p className="text-label font-medium text-ink-2">Nutrition data</p>
          <p className="text-body">
            {foods.without_nutrition === 0
              ? `All ${foods.total} foods have nutrition behind them.`
              : `${foods.without_nutrition} of ${foods.total} foods are waiting on a USDA match. Their weight shows as uncovered in every summary rather than counting as zero.`}
          </p>
          {foods.items.length > 0 && (
            <div className="flex flex-col overflow-hidden rounded-[6px] border border-line">
              {foods.items.map((food) => (
                <button
                  key={food.id}
                  type="button"
                  onClick={() => {
                    setFixed(null);
                    setFixing(food);
                  }}
                  className="flex min-h-[44px] items-center justify-between gap-3 border-b border-line px-3 py-2 text-left last:border-b-0"
                >
                  <span className="min-w-0 flex-1 truncate text-body">
                    {food.canonical_name}
                  </span>
                  <span className="text-label text-ink-2">Find it</span>
                </button>
              ))}
            </div>
          )}
        </Card>
      )}
    </div>
  );
}
