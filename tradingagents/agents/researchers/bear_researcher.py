from tradingagents.agents.context import (
    get_factual_tone_instruction,
    get_instrument_context_from_state,
    get_language_instruction,
    opponent_argument_or_opening,
    report_or_absent,
)


def create_bear_researcher(llm):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = opponent_argument_or_opening(
            investment_debate_state.get("current_response", ""), "bull analyst"
        )
        market_research_report = report_or_absent(state["market_report"], "market")
        sentiment_report = report_or_absent(state["sentiment_report"], "sentiment")
        news_report = report_or_absent(state["news_report"], "news")
        fundamentals_report = report_or_absent(state["fundamentals_report"], "fundamentals")
        instrument_context = get_instrument_context_from_state(state)
        asset_type = state.get("asset_type", "stock")
        target_label = "stock" if asset_type == "stock" else "asset"
        fundamentals_label = (
            "Company fundamentals report"
            if asset_type == "stock"
            else "Asset fundamentals report (may be unavailable for crypto)"
        )

        prompt = f"""You are the Bear Analyst. Your role is to set out the evidence against investing in the {target_label}: risks, challenges, and negative indicators, as the provided research shows them.

Key points to cover:

- Risks and Challenges: Market saturation, financial instability, or macroeconomic factors the reports document that could weigh on the {target_label}.
- Competitive Weaknesses: Market position, innovation, or competitive threats, where the reports give evidence for them.
- Negative Indicators: Financial data, market trends, or recent adverse news, with the figures.
- Bull Points: Respond to the bull analyst's points one by one: confirm, qualify, or contradict each with data, and acknowledge the ones the data supports.
- Style: Plain prose that addresses the bull analyst's points directly.

Resources available:

{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
Conversation history of the debate: {history}
Last bull argument: {current_response}
Use this information to give a factual assessment of the risks and weaknesses of investing in the {target_label} and a data-based response to the bull's points.
""" + get_factual_tone_instruction() + get_language_instruction()

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
