# Demo video script (target 2:40, hard limit 3:00)

Record the screen of the running app. No music. Do not show logos of other companies; text names are fine.

**0:00 to 0:20. Problem.** "Tools that summarize what is known about a gene often write fluent text first and cite later. You can't tell what was actually checked. Claim Ledger flips that: every claim is tied to saved evidence, and unsupported ones are blocked."

**0:20 to 1:05. EGFR (normal use).** Click the EGFR example. "Open Targets gives disease associations with evidence-strength scores, PubMed gives literature counts. Each row in the ledger has an Evidence ID." Open the Evidence tab on one row: "Source, URL, retrieval time, and for co-mentions the exact PubMed query and how PubMed interpreted it." Show the Dossier tab: "This text is built only from verified claims. No language model runs here."

**1:05 to 1:40. Tamper demo (the key moment).** In the Ledger, pick a claim, click the tamper button. "I'm deliberately changing a number." The claim flips to Unsupported with both values. "The evidence never changed. The verifier caught it." Click reset: it returns to Verified.

**1:40 to 2:10. Conflicts on PANK2.** Click PANK2. "A lesser-studied gene. Here the rules fire on real data." Show a Literature_Gap card side by side: "High evidence score, but only a handful of papers actually discuss this pair." Drag a threshold in the sidebar to show it update live.

**2:10 to 2:35. Built with Kiro.** Show the .kiro folder and a passing test run briefly. "Spec-driven build, a steering rule that made Kiro parse real saved responses, two hooks, and property-based tests that caught a real Unicode bug."

**2:35 to 2:50. Close.** "Works offline for four genes, exports the ledger as JSON, and the checking layer could sit behind any AI research assistant. It summarizes evidence, it does not prove causation or give medical advice."

Before recording: run the app fresh, test every click once, close other windows and notifications, zoom the browser to about 125 percent, and keep a dry run under 2:50.
