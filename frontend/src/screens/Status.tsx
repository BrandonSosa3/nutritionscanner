/**
 * How the system itself is doing.
 *
 * Everything here is maintenance rather than daily use, which is why it sits
 * behind one link instead of taking a tab: spend against the ceiling, and the
 * foods still waiting on nutrition data.
 */

import { useEffect, useState } from "react";
import { api, ApiError } from "../api/client";
import type { BudgetStatus, FoodList } from "../api/types";
import { Card, ErrorNote, Skeleton } from "../components/ui";

export function Status() {
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [foods, setFoods] = useState<FoodList | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.budget(), api.foods(true)])
      .then(([budgetStatus, foodList]) => {
        setBudget(budgetStatus);
        setFoods(foodList);
      })
      .catch((caught) =>
        setError(caught instanceof ApiError ? caught.message : "Couldn't load status."),
      );
  }, []);

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

      <Card accent={foods.without_nutrition > 0 ? "attention" : undefined}>
        <p className="text-label font-medium text-ink-2">Nutrition data</p>
        <p className="text-body">
          {foods.without_nutrition === 0
            ? `All ${foods.total} foods have nutrition behind them.`
            : `${foods.without_nutrition} of ${foods.total} foods are waiting on a USDA match. Their weight shows as uncovered in every summary rather than counting as zero.`}
        </p>
        {foods.items.length > 0 && (
          <ul className="flex flex-col gap-1 pt-1">
            {foods.items.slice(0, 8).map((food) => (
              <li key={food.id} className="text-caption text-ink-2">
                {food.canonical_name}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
