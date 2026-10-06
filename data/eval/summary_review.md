# Blind summary review

50 articles. For each, **A** and **B** are the same article summarized by two different models — which one varies per item, so do not look for a pattern.

Fill `verdict` in `summary_review.csv` with **A**, **B**, or **same**. Judge whatever you actually care about: does it carry the right fact, does it read like Sift, would you ship it.

Under each pair, **What the models were given** is the article text exactly as the summarizer received it, alongside the headline above. Use it to check for invented facts and dropped hedges ("allegedly" turned into fact), and note either in the CSV's `notes` column — e.g. `B invented case`. The verdict says which you prefer; only the notes record a fabrication.

Do not open `summary_review.key.json` until you are done.

---

### 1. Trump says record USS Lincoln deployment 'not nearly long enough'
*ABC News*

**A.** President Trump said the record USS Lincoln deployment is 'not nearly long enough' and dismissed concerns about crew conditions, stating that family members are not worried.

**B.** Trump said the USS Lincoln deployment is 'not nearly long enough' and disputed reports that family members are concerned about crew conditions. This is a brief statement with limited detail provided.

<details>
<summary>What the models were given (11 words, teaser only)</summary>

> Trump said USS Lincoln family members are not concerned about conditions.

</details>

---

### 2. Vulnerability giving attackers full control of Macs is under active exploitation
*Ars Technica*

**A.** A high-severity macOS vulnerability (CVE-2026-65400) affecting screen sharing is under active exploitation by attackers who are installing cryptocurrency miners on compromised systems. Apple released patches for the flaw last week.

**B.** A high-severity macOS vulnerability in screen sharing, CVE-2026-65400, is under active exploitation, allowing attackers to execute code and install a Monero miner; Apple has patched it.

<details>
<summary>What the models were given (173 words)</summary>

> Dutch officials have warned that a high-severity macOS vulnerability that allows attackers to execute malicious code is under active exploitation.
>
> “The NCSC has received a notification indicating that active abuse of this vulnerability has been observed on multiple systems on which port 5900 was accessible from the Internet,” the Netherlands National Cyber Security Centrum warned earlier this week. “In all these cases, root had been accessed on the affected system and a Monero crypto miner had been placed.”
>
> Do you know if your screen sharing is on?
>
> The vulnerability, tracked as CVE-2026-65400, received a patch from Apple last week for macOS Tahoe, Sequoia, and Sonoma. The vulnerability, with a severity rating of 7.1 out of 10, stems from a bug in the macOS screen sharing capability, which allows a remote party to view the screen and control the keyboard and mouse while a machine is turned on. A flaw in the “state management,” which keeps track of preceding events, user interactions, variables, and other system states, is the underlying cause.Read full article
>
> Comments

</details>

---

### 3. Trump shrugs off concerns about dismal conditions on USS Abraham Lincoln
*Axios*

**A.** Trump dismissed concerns about poor conditions aboard the USS Abraham Lincoln, which has been deployed for nine months with reports of inadequate supplies, mental health issues, and crew members attempting to go overboard. Lawmakers from both parties have called for investigations into the carrier's deteriorating conditions.

**B.** President Trump dismissed concerns about poor conditions on the USS Abraham Lincoln, including mental health issues and lack of supplies, despite lawmakers calling for investigations; the Navy and Defense Secretary deny the reports.

<details>
<summary>What the models were given (473 words)</summary>

> President Trump said Friday that he isn't concerned about the state of the USS Abraham Lincoln, despite calls from lawmakers to investigate reports of mental health issues for those onboard. Why it matters: The San Diego-based aircraft carrier has been deployed for nine months, and numerous reports have indicated that crew members' families are concerned about a lack of food and other basic supplies. At least one sailor has gone overboard, prompting the Navy to launch an investigation. The Military Times reported that other crew members have also tried to go overboard. What they're saying: Speaking to reporters Friday, Trump said he didn't think the ship had been deployed "nearly long enough." He also disputed that the family members of the 5,000-plus crew were concerned about their well-being, and pointed toward the USS George Washington coming to relieve the Lincoln from its Middle East deployment. While taking questions from the press Friday, a reporter mentioned families were worried, to which Trump replied "no they're not."Zoom out: The Navy Times, The Military Times, and Stars and Stripes were among the first to report on conditions aboard the carrier. The ship hasn't made a port call in at least 200 days. Generally, those occur every 30 to 45 days unless operational demands interrupt plans. Family members of those aboard told The Military Times that the crew lacks basic items like hygiene products and faces other quality-of-life issues, including contaminated water.Low morale and exhaustion are also rampant among the crew, several families said. For the record: In a statement, the Navy denied it had "observed an increase in suicidal ideations or attempts aboard" the USS Abraham Lincoln. "We take every service member's well-being seriously and have religious, medical and mental health professionals available to assess and address concerns as they arise," the statement said. Secretary of Defense Pete Hegseth also called the conditions on the carrier "completely misrepresented." "Some deployments are longer than others, and I have more respect and gratitude for those sailors than anybody. What they do in those high seas and those austere conditions with less port calls, it's incredible," he told reporters Thursday.The White House referred Axios to Trump's comments. Zoom in: Lawmakers from both parties have called for investigations into how conditions on the carrier deteriorated. Sens. Richard Blumenthal (D-Conn.) and Ruben Gallego (D-Ariz.) have both publicly called for probes, with Gallego requesting to visit the ship to see conditions for himself. Between the lines: The situation with the USS Abraham Lincoln reflects a broader pattern among U.S aircraft carriers, which have gone on longer deployments in recent years than they otherwise might. With the war in Iran still going on, resources have been directed into the Middle East and extended time at sea. Another carrier, the USS George H. Bush, deployed on March 31 and has spent at least four...

</details>

---

### 4. Jason Arday, ex-Cambridge professor at centre of plagiarism row, found dead
*BBC*

**A.** Jason Arday, a former Cambridge professor at the center of a plagiarism dispute, was found dead in London on Friday. His family expressed shock at his death.

**B.** Jason Arday, a former Cambridge professor involved in a plagiarism controversy, was found dead in London; his family expressed shock.

<details>
<summary>What the models were given (28 words, teaser only)</summary>

> His family said it was "in shock to have lost this amazing father, partner, brother, uncle and son" after the academic was found dead in London on Friday.

</details>

---

### 5. Hodgkinson beaten to European gold by rival Werro
*BBC Sport*

**A.** Switzerland's Audrey Werro defeated Britain's Keely Hodgkinson in the 800m final at the European Championships, denying Hodgkinson a third consecutive title.

**B.** Keely Hodgkinson was beaten to European gold by Switzerland's Audrey Werro in the 800m final, settling for silver.

<details>
<summary>What the models were given (25 words, teaser only)</summary>

> Keely Hodgkinson is forced to settle for European silver as Switzerland's Audrey Werro denies the Briton a third consecutive title in a thrilling 800m final.

</details>

---

### 6. Instagram accounts fuelling Ceuta crisis with paid advice for help to cross
*BBC World*

**A.** BBC investigation found Instagram accounts offering paid advice and glamorizing illegal border crossings at the Morocco-Ceuta fence. The social media network is fueling migration attempts across this disputed border region.

**B.** BBC's Top Comment podcast found a network of Instagram profiles glamourising the swim around the Morocco-Ceuta border fence, offering paid advice to help people cross. This matters because it highlights how social media is fueling the migration crisis.

<details>
<summary>What the models were given (19 words, teaser only)</summary>

> BBC's Top Comment podcast has found a network of Instagram profiles glamourising the swim around the Morocco-Ceuta border fence.

</details>

---

### 7. Volatile Week for Equity Markets | Closing Bell
*Bloomberg*

**A.** Bloomberg provided comprehensive coverage of the U.S. market close, noting a volatile week for equity markets. This matters as it reflects investor sentiment and market conditions.

**B.** Bloomberg's market coverage program reported on U.S. equity market movements during a volatile trading week. The summary provides no specific market data or newsworthy development.

<details>
<summary>What the models were given (27 words, teaser only)</summary>

> Comprehensive cross-platform coverage of the U.S. market close on Bloomberg Television, Bloomberg Radio, and YouTube with Romaine Bostick, Katie Greifeld, Carol Massar and Tim Stenovec. (Source: Bloomberg)

</details>

---

### 8. Army temporarily grounds Apache helicopter training flights after deadly crash
*CBS News*

**A.** The U.S. Army temporarily halted Apache helicopter training flights after two soldiers were killed in a crash near Fort Hood, Texas. This matters as it raises safety concerns in military training.

**B.** The U.S. Army temporarily halted Apache helicopter training flights after two soldiers were killed in a crash near Fort Hood, Texas earlier this week. The pause is a safety precaution following the fatal incident.

<details>
<summary>What the models were given (28 words, teaser only)</summary>

> The U.S. Army said Friday it is temporarily halting Apache helicopter training flights after two soldiers were killed earlier this week​ in a crash near Fort Hood, Texas.

</details>

---

### 9. UFC 330: Expect fireworks in strawweight title bout between Mackenzie Dern and Gillian Robertson
*CBS Sports*

**A.** UFC 330 will feature a strawweight title bout Saturday in Philadelphia between submission specialists Mackenzie Dern and Gillian Robertson. Both fighters have promised an action-packed match.

**B.** UFC 330 features a strawweight title bout between Mackenzie Dern and Gillian Robertson, both submission specialists who promise action. This matters for fight fans as it's a highly anticipated matchup.

<details>
<summary>What the models were given (15 words, teaser only)</summary>

> The pair of submission specialists have promised to deliver action on Saturday night in Philly

</details>

---

### 10. OpenAI talent exodus raises 'huge red flag' ahead of IPO
*CNBC*

**A.** OpenAI's C-suite turnover raises a 'huge red flag' for investors ahead of its anticipated IPO. This matters because it signals potential instability at a leading AI company.

**B.** OpenAI is experiencing significant executive turnover that raises investor concerns as the company prepares for an IPO. The departures are viewed as a warning sign ahead of the planned public offering.

<details>
<summary>What the models were given (17 words, teaser only)</summary>

> OpenAI's C-suite turnover gives investors another reason for concern as the company pushes toward a mammoth IPO.

</details>

---

### 11. Data centers are officially shaping election season
*Canary Media*

**A.** Data centers have become a major electoral issue ahead of the 2026 elections as public frustration with them grows. The topic is reshaping election-season politics across regions.

**B.** Data centers have become a contentious political issue, with public frustration influencing the 2026 election season. The article analyzes how this topic is shaping campaigns and policy debates.

<details>
<summary>What the models were given (51 words)</summary>

> This analysis and news roundup come from the Canary Media Weekly newsletter.  Sign up to get it every Friday.   The data center elections are officially upon us.  Public frustration with data centers has been simmering, and in recent months it has become increasingly clear that the topic would reshape the 2026…

</details>

---

### 12. Q&A: What does China’s 15th five-year plan for coal mean for climate action?
*Carbon Brief*

**A.** China's new five-year coal plan for 2026-2030 sets a goal to peak coal consumption during that period but stops short of naming a specific year, emphasizing coal's continued importance in its energy system ahead of its broader 2030 carbon emissions peak commitment. The plan matters because it signals how China will manage its transition away from coal while still relying on it as a major energy source.

**B.** China's new five-year plan for coal (2026-2030) does not set a specific peak year for coal consumption, instead aiming to peak within the period, while emphasizing coal's role in the energy system and its 'green and low-carbon transition.' This matters for global climate action as China is the largest emitter.

<details>
<summary>What the models were given (167 words)</summary>

> Print article   Share    China has published a new five-year plan for coal, the latest in a slew of important policy documents for the country’s energy transition. The 15th five-year plan for the development of the coal industry was published by the National Development and Reform Commission (NDRC) and the National Energy Administration (NEA) on 10 August, covering the period 2026-2030.&nbsp; This is a key period, covering the years building up to China’s pledge to peak its carbon dioxide (CO2) emissions “before 2030”. Government-affiliated organisations had previously mooted the possibility of coal consumption peaking before 2027. However, the new plan does not set a specific, government-endorsed year for peaking coal consumption, instead including a broader goal to peak use of the fuel in this five-year period. It also discusses the “green and low-carbon transition” of the coal industry, coal-related methane emissions and the “clean and efficient use” of the fuel. But, in general, the plan emphasises the importance of coal in China’s energy system and focuses on the...

</details>

---

### 13. David Ellison’s Friday ParaBros News Dump: CEO Complains About “Needless Costs” Of State AGs’ Antitrust Suit As Mexico Approves Merger
*Deadline*

**A.** Paramount CEO David Ellison complained about the costs of state antitrust lawsuits challenging the Paramount-Warner Bros. Discovery merger, which Mexico has now approved. The article covers corporate leadership's public pushback against regulatory obstacles to the $111 billion deal.

**B.** Paramount CEO David Ellison criticized state attorneys general's antitrust lawsuit as 'needless costs' while Mexico approved the merger with Warner Bros. Discovery. The $111 billion deal faces regulatory hurdles but gains international approval.

<details>
<summary>What the models were given (56 words)</summary>

> In case you didn&#8217;t already know, Paramount CEO David Ellison really wants to make sure this afternoon that everybody is clear those pesky state Attorney Generals and their antitrust lawsuit are ruining a really good $111 billion thing &#8212; and now even Mexico agrees with him. &#8220;Paramount and WBD could and would close today and [&#8230;]

</details>

---

### 14. Kurkjian: What did we learn from the 1994 MLB strike? Five lessons
*ESPN*

**A.** As MLB faces another potential lockout, ESPN examines five lessons from the 1994 players' strike, baseball's last major work stoppage. The article provides historical context for understanding current labor tensions in professional baseball.

**B.** The article reflects on lessons from the 1994 MLB strike as another lockout looms, drawing parallels to the current labor situation. It highlights five key takeaways from the past work stoppage.

<details>
<summary>What the models were given (17 words, teaser only)</summary>

> As another lockout looms, what lessons can we take from Major League Baseball's last prolonged work stoppage?

</details>

---

### 15. Jane Street suffers $15bn loss in July market ructions
*Financial Times*

**A.** Jane Street, a Wall Street trading firm, lost $15 billion in July due to market volatility, though it still recorded strong trading revenues for 2026. The loss underscores the impact of market ructions on major trading firms.

**B.** Trading firm Jane Street recorded a $15 billion loss during July market volatility but has still posted strong trading revenues in 2026. The article highlights how major financial firms navigate turbulent market conditions.

<details>
<summary>What the models were given (12 words, teaser only)</summary>

> Wall Street trading firm has still recorded hefty trading revenues in 2026

</details>

---

### 16. What To Watch This Weekend: New Shows And Movies To Stream On Netflix, Hulu, Prime Video, Apple TV And More
*Forbes*

**A.** A roundup of new movies and shows available to stream on Netflix, Hulu, Prime Video, Apple TV, and other platforms this weekend.

**B.** Forbes previews new movies and shows available this weekend across streaming platforms including Netflix, Hulu, Prime Video, and Apple TV.

<details>
<summary>What the models were given (24 words, teaser only)</summary>

> Looking for something new to stream this weekend? Here’s every major new movie and show hitting Netflix, Hulu, Prime Video, Apple TV and more.

</details>

---

### 17. Trump’s Flurry of National Security Policies
*Foreign Policy*

**A.** The Trump administration announced new national security policies covering drones, cyberwarfare, and shipbuilding.

**B.** The Trump administration announced new national security policies related to drones, cyberwarfare, and ship building.

<details>
<summary>What the models were given (10 words, teaser only)</summary>

> The administration made moves on drones, cyberwarfare, and ship building.

</details>

---

### 18. Trump’s fight over rarely used 18th-century deportation law lives on in latest court clash
*Fox News*

**A.** A federal appeals court dismissed a challenge to Trump's use of the Alien Enemies Act for deportations, leaving the law's legality unresolved; the case was deemed moot after the plaintiffs were removed under other authorities.

**B.** A federal appeals court dismissed as moot a challenge to Trump's use of the 18th-century Alien Enemies Act to deport alleged gang members, leaving the law's legality unresolved. The court's decision vacates a prior ruling against Trump's invocation while dodging the merits question that the Supreme Court may decide in future cases.

<details>
<summary>What the models were given (477 words)</summary>

> The Fifth U.S. Circuit Court of Appeals on Thursday dismissed as moot a challenge to President Donald Trump’s use of the Alien Enemies Act to deport alleged Tren de Aragua members, leaving the legality of his invocation of the 18th-century law unresolved.The New Orleans-based court said the case became moot after all three Venezuelan plaintiffs, whom the administration alleged were members of Tren de Aragua, had already been removed from the United States under other immigration authorities.While the Alien Enemies Act dates back hundreds of years, prior to Trump, it was most recently invoked by President Harry Truman in 1946. The law allows the president, under specified wartime or invasion circumstances involving a foreign nation or government, to detain and remove certain non-naturalized individuals of that hostile power.The Trump administration has argued that Tren de Aragua’s gang activity amounts to an "invasion or predatory incursion" under the law and has sought to use the authority as part of its broader immigration agenda, including efforts to speed the removal of suspected gang members.DC APPEALS COURT ORDERS JUDGE BOASBERG TO HALT TRUMP CONTEMPT PROBE OVER DEPORTATION FLIGHTSThe Supreme Court previously blocked the administration from removing the detainees under the Alien Enemies Act while the case proceeded, but stopped short of deciding whether Trump had lawfully invoked the statute, sending the dispute back to the Fifth Circuit.Advancing American Freedom senior legal fellow Bryce Poole described the ruling as a mixed result for the Trump administration."The Fifth Circuit's en banc decision in W.M.M. v. Trump represents one step forward, one step sideways for the Trump Administration," Advancing American Freedom senior legal fellow Bryce Poole told Fox News Digital. "Last year, in A.A.R.P. v. Trump, the Supreme Court blocked the removals but declined to decide whether President Trump's invocation of the Alien Enemies Act was lawful, sending that question back to the Fifth Circuit."Advancing American Freedom is a conservative public policy advocacy organization founded by former Vice President Mike Pence."It's a step forward because it vacates the prior ruling that said Trump's invocation was unlawful, leaving the President's AEA powers intact," Poole explained. "It's a step sideways because the court dodged the merits, so the AEA's legality remains a live question the Supreme Court will likely decide — probably in a different case like J.A.V. v. Trump, which has a certified class, so mootness won't apply."BIDEN JUDGE OVERRULED ON KEY TRUMP IMMIGRATION POLICYEven though the court declined to rule on the merits, two judges signaled their belief that the president’s use of the law was appropriate in their concurring opinions."I agree that this case is moot," Judge James Ho wrote. "But I also agree with the United States that we should address the merits questions directed to us by the Supreme Court — and affirm the President’s actions under the Alien Enemies Act and the Due Process Clause.""As I’ve also noted, judges...

</details>

---

### 19. The Download: Flock’s new rules, cloning’s future, and children’s cells
*MIT Tech Review*

**A.** This tech newsletter covers Flock's new restrictions on police access to license plate readers amid surveillance backlash, a CRISPR technique to create female clones from male mice, and efforts to map children's cells, plus a military exercise where Ukrainian drones defeated US forces.

**B.** Police-tech company Flock is tightening access rules for its license plate reader database in response to surveillance concerns and misuse for stalking, though potential loopholes remain. The article also covers recent advances in cloning technology and efforts to map children's cells for medical research.

<details>
<summary>What the models were given (482 words)</summary>

> This is today&#8217;s edition of The Download, our weekday newsletter that provides a daily dose of what&#8217;s going on in the world of technology. Flock is tightening its rules in response to a growing surveillance backlash The police-tech giant Flock is changing officers’ access to its nationwide network of license plate readers. The move comes amid a backlash over mass surveillance and reports of officers using the technology to stalk and harass current or former romantic partners. To combat that, the company will require them to enter a criminal case number before searching its database and expand automated auditing of suspicious searches. But because Flock won’t verify those case numbers, officers could still find ways around the safeguards. Here’s what Flock is changing—and where loopholes remain. —James O&#8217;Donnell Cloning could be used to save species—or make human “organ sacks” —Jessica Hamzelou This week I spoke to scientists who have found a way to turn male mouse embryos female. They’ve developed a CRISPR-based approach to essentially cut out the Y chromosome. It allowed them to create female clones of male mice. They hope their approach could be helpful in conservation efforts, especially in cases where we might have only a few individuals of a species left. But cloning has multiple uses, ranging from genetically modifying livestock to recreating beloved pets and potentially even creating “brainless” replicas of humans. Find out what cloning can do now, and where it could lead next. This story is from The Checkup, our weekly biotech newsletter. Sign up to receive it in your inbox every Thursday. This scientist is helping build a missing map of childhood In 2017, Deanne Taylor attended a presentation about the Human Cell Atlas, an ambitious attempt to map every cell in the human body. Taylor was floored, and then concerned. The project’s researchers had only made plans to study adults. “That’s when my little alarm went off,” she says. “Not again.” Children’s cells are different from grownups’ cells in the way they express genes, which can cause drastically different and even deadly responses to drugs that adults tolerate well. Taylor has since pushed the Human Cell Atlas to include children and is working on a major database of healthy pediatric tissue. The goal is to give researchers a baseline for how children develop—and potentially reveal how diseases that emerge in adulthood begin much earlier. Meet the scientist building a cellular map of childhood. —Colleen de Bellefonds This story is from the next issue of our print magazine, which is all about kids. Subscribe now to read it when it lands. The must-reads I’ve combed the internet to find you today’s most fun/important/scary/fascinating stories about technology. 1 Ukrainian drones defeated US forces in a military exerciseThey wiped out an American tank brigade in the war game. (WSJ $)+ The drill exposed US vulnerabilities to drone attacks. (Ars Technica)+ Trump just declared 100% tariffs on...

</details>

---

### 20. How Americans deceive ourselves about slavery
*NPR*

**A.** NPR interviews author Clint Smith about his works exploring how slavery is taught and remembered in America.

**B.** Clint Smith discusses his books on slavery and its teaching, including How the Word Is Passed and Above Ground, in interviews with Fresh Air.

<details>
<summary>What the models were given (40 words)</summary>

> Clint Smith's How the Word Is Passed explores how slavery is — and isn't — taught. And Above Ground includes poems to his children about what their ancestors endured and escaped. He spoke with Fresh Air in 2021 and 2023.

</details>

---

### 21. Record cyclosporiasis outbreak tests agencies hit by Trump administration's cuts
*NPR Health*

**A.** A record cyclosporiasis outbreak in the U.S. is testing federal agencies that oversee foodborne illness, which have been impacted by Trump administration funding cuts. The outbreak highlights the real-world consequences of reduced resources for disease outbreak response.

**B.** A record cyclosporiasis outbreak is testing federal agencies that were already weakened by funding cuts from the Trump administration, raising concerns about foodborne illness response.

<details>
<summary>What the models were given (23 words, teaser only)</summary>

> Last year's funding cuts to federal agencies that oversee foodborne illness outbreaks are being tested with the record cyclosporiasis outbreak in the U.S.

</details>

---

### 22. New aircraft carrier heads toward Mideast after reports of issues on long-deployed USS Lincoln
*NPR World*

**A.** The USS George Washington is being deployed to the Middle East as the long-deployed USS Abraham Lincoln faces reported mental health and supply issues among its crew. The carrier swap addresses operational concerns aboard the Lincoln.

**B.** The USS George Washington is heading to the Middle East as reports surface of mental health and supply issues on the long-deployed USS Abraham Lincoln, highlighting strain on naval forces.

<details>
<summary>What the models were given (30 words, teaser only)</summary>

> The Pacific-based aircraft carrier USS George Washington has begun heading toward the Middle East as reports have emerged of mental health and supply issues aboard the long-deployed USS Abraham Lincoln.

</details>

---

### 23. El-Sayed Shouldn’t Blame Muslim America for His Radical-Islam-Curious Views
*National Review*

**A.** National Review argues that Michigan Senate candidate El-Sayed and his supporters are incorrect to characterize his views on radical Islam as uncontroversial within Muslim communities. The opinion piece contests the framing of his statements by his defenders.

**B.** An opinion piece argues that Michigan Senate candidate El-Sayed's controversial views on radical Islam are not representative of Muslim Americans, contradicting his defenders' claims.

<details>
<summary>What the models were given (22 words, teaser only)</summary>

> The Michigan Senate candidate and his defenders are wrong to imply that his views and statements are uncontroversial among his fellow Muslims.

</details>

---

### 24. Author Correction: Cell intrinsic immunity spreads to bystander cells via the intercellular transfer of cGAMP
*Nature*

**A.** A correction has been published for a Nature paper on cell intrinsic immunity spreading via cGAMP transfer, addressing an error in the original research.

**B.** Nature published an author correction to a peer-reviewed article about cell intrinsic immunity and intercellular transfer of cGAMP. The correction addresses errors in the previously published research.

<details>
<summary>What the models were given (21 words, teaser only)</summary>

> Nature, Published online: 14 August 2026; doi:10.1038/s41586-026-11023-3Author Correction: Cell intrinsic immunity spreads to bystander cells via the intercellular transfer of cGAMP

</details>

---

### 25. Inside Page Six’s VRT Out East party at Barlume Beach in Montauk with the OG ‘RHONY’ Housewives, Hilaria Baldwin and more
*New York Post*

**A.** Page Six hosted a 'VRT Out East' party in Montauk featuring original RHONY housewives and Hilaria Baldwin, a celebrity social event.

**B.** Page Six covered a "Virtual Reali-Tea" party held in Montauk that featured Real Housewives of New York cast members and celebrity attendees including Hilaria Baldwin. The event was a social gathering at a beach venue in the Hamptons.

<details>
<summary>What the models were given (19 words, teaser only)</summary>

> "Virtual Reali-Tea" took a trip to the Hamptons for the "VRT Out East" party at Barlume Beach in Montauk.

</details>

---

### 26. Stock Trading in Congress Becomes an Attack Line in Midterm Campaigns
*New York Times*

**A.** Both parties are using congressional stock trading as a campaign attack, reflecting voter anger over the practice.

**B.** Both Republican and Democratic challengers are using congressional stock trading as an attack line in midterm campaigns, capitalizing on voter anger over the practice. The issue has become a common bipartisan campaign talking point despite being a widespread practice among lawmakers.

<details>
<summary>What the models were given (24 words, teaser only)</summary>

> Republican and Democratic challengers are both using the issue to attack members of Congress as unethical, seizing on a common practice that enrages voters.

</details>

---

### 27. Ancient Nero's Bridge reemerges from Tiber River as Rome faces water crisis
*PBS NewsHour*

**A.** The ancient bridge, named for Nero, has reemerged from the Tiber River as Rome faces a water crisis, highlighting the drought's impact.

**B.** An ancient bridge named after Roman Emperor Nero has re-emerged from the Tiber River as Rome faces a water crisis. The bridge once connected Rome's city center to the river's right bank.

<details>
<summary>What the models were given (18 words, teaser only)</summary>

> Named for the former Roman emperor, the bridge once connected Rome's city center with the Tiber's right bank.

</details>

---

### 28. Watch Julia Jacklin Cover Sugar Ray’s “Every Morning”
*Pitchfork*

**A.** Musician Julia Jacklin performed a cover of Sugar Ray's "Every Morning." The cover demonstrates Jacklin's interpretation of the song.

**B.** Julia Jacklin covers Sugar Ray's 'Every Morning,' saying the song makes her feel grateful to be alive.

<details>
<summary>What the models were given (10 words, teaser only)</summary>

> “The song just makes me feel grateful to be alive”

</details>

---

### 29. The nation’s cartoonists on the week in politics
*Politico*

**A.** A weekly roundup of political cartoons from across the country capturing the week's political events.

**B.** Political cartoonists from across the country and political spectrum created cartoons this week responding to events in politics. The cartoons capture various political developments and foibles.

<details>
<summary>What the models were given (68 words)</summary>

> Every week political cartoonists throughout the country and across the political spectrum apply their ink-stained skills to capture the foibles, memes, hypocrisies and other head-slapping events in the world of politics. The fruits of these labors are hundreds of cartoons that entertain and enrage readers of all political stripes. Here's an offering of the best of this week's crop, picked fresh off the Toonosphere. Edited by Matt Wuerker.

</details>

---

### 30. Russell Fry backs Darline Graham for Senate in South Carolina
*Politico Congress*

**A.** Rep. Russell Fry endorses Darline Graham in the South Carolina Senate runoff, a key endorsement after a contentious primary.

**B.** GOP Rep. Russell Fry endorsed Sen. Darline Graham in a South Carolina Republican primary runoff against Rep. Ralph Norman scheduled for August 25. Fry's endorsement is strategically significant as he previously competed against Graham in the initial primary and carries influence in the conservative coastal region.

<details>
<summary>What the models were given (347 words)</summary>

> GOP Rep. Russell Fry threw his support behind Sen. Darline Graham (R-S.C.) for a full term in the Senate — a key endorsement for the political newcomer who’s locked in a heated primary runoff with Rep. Ralph Norman (R-S.C.).
>
> “On August 25, I’m asking everyone who supported our campaign to join me in supporting Darline Graham for the United States Senate,” Fry said in a statement Friday.
>
> Fry, who unsuccessfully ran in the snap primary to replace late Sen. Lindsey Graham as the Republican nominee on the November ballot, came in third place during the first round of voting. His support for Graham — who entered the race with President Donald Trump’s early backing — will be fundamental in helping her shore up votes in his deeply conservative coastal home region, potentially countering Norman’s dominance in Upstate South Carolina.
>
> It’s a notable shift after Graham and Fry traded fierce barbs in the leadup to the Aug. 11 primary, primarily through attack ads sponsored by their campaigns and outside allies.
>
> Fry claimed Graham “ballooned the size” and doubled the spending of the South Carolina Commission for the Blind, which she has led since 2019. Security in Strength, a Graham-aligned PAC, blanketed local TV with an ad that shows an AI-depiction of Fry’s likeness transposed onto a slice of meat in a frying pan being dumped out by Trump.
>
> Now, Graham and Norman are racing to win influential endorsements from powerbrokers across the state to build a majority coalition ahead of the Aug. 25 runoff.
>
> Graham has received the backing of fellow South Carolina Sen. Tim Scott as well as Lt. Gov. Pamela Evette and GOP nominee for Agriculture Commissioner Cody Simpson — both close allies of Trump and Gov. Henry McMaster. Norman has banked the support of several senators — Sen. Rick Scott of Florida and Sen. Mike Lee of Utah — as well as former U.N. Ambassador and South Carolina Gov. Nikki Haley.
>
> Former Gov. Mark Sanford, who also ran in the Senate primary and failed to make the runoff, has not yet publicly backed a candidate.

</details>

---

### 31. How Trump’s Unprecedented Effort to Prosecute Noncitizen Voters Fell Apart
*ProPublica*

**A.** ProPublica's investigation reveals that the Trump administration's effort to prosecute noncitizen voters, led by federal agencies including Homeland Security Investigations, has yielded minimal results despite substantial resources, with one U.S. attorney's office finding only one prosecution referral out of approximately 130 investigated cases. The story matters because it documents the gap between the administration's public claims about widespread illegal voting by noncitizens and the actual investigative findings.

**B.** An internal email from Minnesota's U.S. attorney reveals that the Trump administration's push to prosecute noncitizen voters was disorganized and yielded only one referral out of 130 subpoenaed records, despite heavy resources. The story matters because it exposes the meager results of a high-priority election fraud initiative.

<details>
<summary>What the models were given (455 words)</summary>

> Illustration by Matt Rota for ProPublica. Animation by Henrike Lendowski for ProPublica. It was late March when Joe Teirab, the second-in-command at Minnesota&#8217;s U.S. attorney&#8217;s office, received an urgent email from Washington. The federal government was scrambling to find criminal cases to back up President Donald Trump&#8217;s claims that illegal voting by noncitizens was tipping the scales in American elections. Agents from Homeland Security Investigations, a massive federal law enforcement agency, had been dispatched to work leads across the country, including hundreds in Minnesota. Teirab was already under pressure. In an earlier missive, Nick Davis, a high-ranking Justice Department appointee helping to lead the election fraud crusade, had reminded him the cases were so high priority that Teirab and his staff couldn&#8217;t decline to move forward on them without express approval from agency higher-ups. On March 24, Davis demanded a status report — within hours. Teirab, a former Marine and a Harvard Law graduate who’d run unsuccessfully for Congress as a Republican, responded with a blunt reality check. “Bottom line up front,” he replied in an email reviewed by ProPublica. After subpoenaing records on about 130 people, only one had been referred for prosecution, his staff had told him. Agents had deluged local election offices with calls and demands for voting histories, demonstrating “a complete lack of understanding” of illegal voting investigations. “The HSI task force has been disjointed and disorganized,” Teirab wrote. The entire process, he said, had been “dysfunctional.” Since Trump regained the White House, his administration has launched a series of unprecedented initiatives to find and prosecute voting by noncitizens, which he’s long claimed, without evidence, is rampant. He’s stepped up this push in recent weeks, saying in a nationally televised speech that the American election system was “so vulnerable that no one can possibly defend it.” To support that assertion, the Department of Homeland Security, HSI’s parent agency, released documents asserting it had found more than 250,000 noncitizens on voter rolls in just four states, all led by Democrats. The documents included no explanation of how that number was calculated. It’s well known the administration has tasked HSI — a force established to combat drug cartels, terrorism and other cross-border criminal enterprises — with leading the campaign to find election fraud cases in the United States. But an investigation by ProPublica reveals for the first time how the Trump administration came to harness HSI’s personnel, technology and sweeping legal authority in service of its election agenda — and how meager the results have been, despite the prodigious resources sunk into the effort. According to interviews and internal emails reviewed by ProPublica, career staffers at the Justice Department warned that transferring voter rolls to HSI to enable it to search for noncitizen...

</details>

---

### 32. Colombia's New President Wants More U.S. Military Help Fighting Cartels
*Reason*

**A.** Colombia's new president has joined the U.S.-led 'Shield of the Americas' coalition to combat drug cartels, continuing a failed counternarcotics policy that has seen record cocaine production. The move expands the U.S. military's role in Latin America's drug war.

**B.** Colombia's new president has joined the Trump administration's "Shield of the Americas" counter-cartel coalition and pledged intensified military action against drug trafficking, continuing a decades-long U.S.-backed approach that critics argue has failed to reduce cocaine production. The story matters because it shows how the Trump administration is expanding its military involvement in Latin America's drug war despite historically limited results.

<details>
<summary>What the models were given (394 words)</summary>

> The Trump administration is expanding the footprint of its war on drugs in Latin America. On Wednesday, Defense Secretary Pete Hegseth announced that Colombia would join the "Shield of the Americas" initiative. When the multinational security coalition of the U.S. and Latin American countries formed in March, President Donald Trump said the group was united by a commitment to use "lethal military force to destroy the sinister cartels and terrorist networks once and for all."  Hegseth said members of the Shield, a.k.a. the Americas Counter Cartel Coalition, will take "concrete steps—sometimes risky steps, courageous steps—in partnership" toward its goal of "dismantling the drug cartels that threaten safety, security and sovereignty in the Western Hemisphere." To date, at least 19 countries in the region have signed on to the coalition, including the Dominican Republic, Trinidad and Tobago, and Ecuador, which have all had citizens killed by the Trump administration's extrajudicial maritime strikes, according to the Washington Office on Latin America, a human rights research and advocacy organization. Colombia's entry into the group came at the request of its new president, Abelardo de la Espriella, who on the campaign trail promised to use the "air force, the army, and the police" to neutralize every plane and boat "loaded with drugs that leaves Colombia." Moments after taking office last week, the new president doubled down, pledging to "relentlessly defeat narcoterrorism and every criminal organization that threatens Colombia's freedom," according to The Wall Street Journal.  He has wasted no time fulfilling his promise. On Sunday, Colombian forces killed four members of the former Revolutionary Armed Forces of Colombia and two members of Colombia's largest drug cartel in two military operations across the country, according to the Miami Herald. In a way, Espriella is simply continuing the same failed policies as his predecessors. Since the launch of Plan Colombia in 2000—a counternarcotics initiative to train, equip, and assist the Colombian military financed by the U.S.—America has given Colombia billions in foreign aid, with minimal results.  Last September, the Trump administration found Colombia had "failed demonstrably to meet its drug control obligations." It certified it as a "major drug transit" and one of the "major illicit drug producing countries," a designation it has carried for over 40 years. Cocaine production in Colombia has reached record levels, "nearly nine times what United Nations researchers say was produced in 2012," reports The...

</details>

---

### 33. Elvis Presley: His Best Country Songs
*Rolling Stone*

**A.** A Rolling Stone listicle highlights Elvis Presley's best country songs, showcasing his interpretations of classics by Hank Williams and others. It's a pop culture feature for music fans.

**B.** Rolling Stone highlights Elvis Presley's interpretations of country music classics by artists including Hank Williams, Ray Price, and George Jones.

<details>
<summary>What the models were given (15 words, teaser only)</summary>

> The immortal vocalist's interpretations of classics by Hank Williams, Ray Price, George Jones, and more

</details>

---

### 34. Epic’s alleged anticompetitive practices under scrutiny from federal, state investigators
*STAT News*

**A.** The Federal Trade Commission is investigating Epic Systems Corp., the nation's largest electronic health records vendor, for potential antitrust violations related to its dominant market position and business practices. The probe is in early stages and may never result in charges, but addresses ongoing complaints about the company's policies.

**B.** The FTC is investigating Epic Systems, the largest electronic health records vendor, for potential antitrust violations, though the probe is early and may not lead to charges. The inquiry focuses on complaints from rivals and former employees about Epic's business practices.

<details>
<summary>What the models were given (126 words)</summary>

> The Federal Trade Commission is examining Epic Systems Corp., the nation&#x2019;s largest vendor of electronic health records, for potential violations of antitrust law as part of a broad inquiry into the company&#x2019;s business practices, according to three people who were recently contacted by investigators.
>
> The probe is in its early stages and may never lead to charges against Epic, whose dominant market position and control of Americans&#x2019; health data has rapidly accelerated in recent years. But the people contacted by investigators &#x2014; who work in or advise health care businesses that interface with Epic &#x2014; said they were asked about a wide range of issues relating to company policies and practices that have generated continual complaints and lawsuits from former employees and rival companies.Read the rest&hellip;

</details>

---

### 35. Drought may have driven Brazil’s first yellow fever outbreak in nearly 80 years
*Science (AAAS)*

**A.** Brazil's first yellow fever outbreak in nearly 80 years may have been driven by drought, which forced forest-dwelling primates into cities, bringing mosquitoes and disease. The finding highlights how climate conditions can trigger disease spread.

**B.** A drought in Brazil forced forest primates into cities, spreading yellow fever mosquitoes and triggering Brazil's first outbreak of the disease in nearly 80 years. The outbreak illustrates how environmental conditions can drive disease emergence in urban areas.

<details>
<summary>What the models were given (13 words, teaser only)</summary>

> Dry conditions forced forest-dwelling primates into the cities, bringing with them mosquitoes—and disease

</details>

---

### 36. Trump on Long-Suffering Sailors: “Not Nearly Long Enough”
*Slate*

**A.** Trump and his team are denying that sailors are suffering, according to Slate's report. The story matters as it reflects disputed claims about military personnel welfare.

**B.** President Trump reportedly said that sailors' suffering is 'not nearly long enough,' and his team denies the claim, sparking controversy.

<details>
<summary>What the models were given (10 words, teaser only)</summary>

> Sailors are suffering. Trump and his team are denying it.

</details>

---

### 37. Mariners Probable Starters vs. Astros for Aug. 14–16 Series
*Sports Illustrated*

**A.** The Seattle Mariners face the Houston Astros in an Aug. 14-16 series where Seattle has dominated this season, though Houston has made roster changes since their last meeting. This covers upcoming MLB matchups between AL West rivals.

**B.** The Seattle Mariners have dominated the Houston Astros this season, but the Astros have changed significantly since their last meeting, setting up a key series.

<details>
<summary>What the models were given (19 words, teaser only)</summary>

> Seattle has dominated Houston this season, but the Astros have changed considerably since the AL West rivals last met.

</details>

---

### 38. Eight Perfect Series Finales
*The Atlantic*

**A.** The Atlantic highlights eight TV series with acclaimed finales, including shows like Fleabag and Twin Peaks that successfully concluded despite the difficulty of satisfying audiences with final episodes. The story matters because perfect endings are rare and culturally significant in television.

**B.** The Atlantic lists eight TV shows with perfect series finales, including Fleabag and Twin Peaks, analyzing why their endings work.

<details>
<summary>What the models were given (494 words)</summary>

> This is an edition of The Atlantic Daily, a newsletter that guides you through the biggest stories of the day, helps you discover new ideas, and recommends the best in culture. Sign up for it here.Last year, I asked The Atlantic’s writers and editors to name a perfect episode of TV, but a perfect finale is especially hard to pull off—and nothing invokes the wrath of fans quite like a bad ending. Here are eight shows that make the most out of their last minutes.The following contains spoilers for the shows mentioned.Fleabag (streaming on Prime Video)Almost nobody is left happy by the final moments of Fleabag—not the heartbroken but resolute “Hot Priest”; not the protagonist Fleabag, watching a fox trail behind the man who has just left her for God; and certainly not the viewer, who has no choice but to let Fleabag look into the camera, shake her head, and leave us behind.But the best ending is a cathartic one. Fleabag, played by the show’s creator, Phoebe Waller-Bridge, is a dry-humored, unabashed, fourth-wall-breaking character who encounters God and new meaning in the second season, which is also the last. She attends a Quaker meeting and is moved by the Holy Spirit to say one of my favorite lines of the series: “I sometimes worry I wouldn’t be such a feminist if I had bigger tits.” She and her sister, Claire, repair their relationship and each kneel, exasperated, before different men to ask for deliverance—Claire from her obnoxious husband, and Fleabag from her suffering. And in the last moments of the finale, after a backyard wedding and the implosion of Claire’s marriage, Fleabag tells the Hot Priest that she loves him, and he tells her, “It’ll pass.” To anybody still reeling from the conclusion of this perfectly short-lived show, that’s also a message to us: This is the end, and one day it will feel like enough.— Stephanie Bai, senior associate editor***Twin Peaks (streaming on Pluto TV and Paramount+)Twin Peaks had multiple conclusions over its strange life as a TV show, first ending on a shocking second season cliff-hanger in 1991, then failing to resolve that with a follow-up film a year later, leaving fans feeling frustrated by what could have been. But in 2017, the co-creators David Lynch and Mark Frost delivered a magnificent and challenging third season with an even more mind-bending last episode. In the true finale of Twin Peaks, titled “Part 18” or “What Is Your Name?,” the heroic Special Agent Dale Cooper (played by Kyle MacLachlan) finally frees himself from the demonic possession that he’s been under for the entire season and travels back in time to the start of the show to try to undo Laura Palmer’s murder. Whatever he does, however, creates an entirely new world that seems even more confusing, and the screen cuts to black as some new version of Laura lets out a chilling scream. It’s another dramatic cliff-hanger, but this time a more...

</details>

---

### 39. Massachusetts Mayor Charged In Alleged $1,500,000 Pandemic-Era Loan Fraud And Money Laundering Scheme
*The Daily Caller*

**A.** A Massachusetts mayor has been charged in an alleged $1.5 million pandemic-era loan fraud and money laundering scheme. The charges relate to accusations of corruption and fraudulent activity during COVID relief efforts.

**B.** A Massachusetts mayor has been charged in an alleged $1.5 million pandemic-era loan fraud and money laundering scheme, with prosecutors citing alleged corruption and lies.

<details>
<summary>What the models were given (4 words, teaser only)</summary>

> ‘alleged corruption and lies’

</details>

---

### 40. Flock Announces New Privacy Protocols After Populist Backlash Against Cameras
*The Daily Wire*

**A.** Flock Safety, a surveillance camera company, announced new privacy protocols including shorter data retention and restricted access for other agencies, in response to public backlash and concerns about misuse.

**B.** Flock Safety announced privacy protocol changes for its surveillance camera system following public backlash, including allowing police departments to restrict data access and reducing data retention from 30 days to one week. The update reflects growing concerns about surveillance technology use and misuse by law enforcement.

<details>
<summary>What the models were given (300 words)</summary>

> Flock Safety, whose surveillance cameras have faced mounting questions, announced Thursday a set of changes designed to improve privacy protections and reduce the likelihood of misuse.
>
> The company said the updates emerge in response to concerns that have built over time about how the technology is used, according to Just The News.
>
> Under the new approach, individual police departments will be able to limit information other law enforcement agencies can access. One possible arrangement would allow outside agencies to search for data on stolen vehicles while blocking them from examining records connected to immigration violations, as described in a New York Times report.
>
> Flock is also changing how long the gathered material remains available. The standard period will drop from 30 days to one week. Local police departments will have the option to extend the time period if they decide it is necessary for an investigation.
>
> Chad Marlow, who serves as senior policy counsel at the American Civil Liberties Union, called the steps &#8220;good in theory&#8221; when he spoke with the Times.
>
> He went on to say the company appears to be treating privacy questions largely as a public relations concern. Other critics have taken a firmer line, arguing that only actual changes in legislation can place real limits on this form of surveillance technology.
>
> At present, several thousand law enforcement agencies rely on Flock’s system. That system includes about 120,000 cameras placed in communities across the country. The devices scan license plates as cars drive past and can monitor potential criminal activity.
>
> Reports of improper access, along with broader unease about the monitoring of ordinary drivers, have led to protests in multiple locations. Some cities have already moved to end their contracts with the company as a result.
>
> The announcement arrives while public discussion of the cameras remains active.

</details>

---

### 41. A Pre-Election Crackdown in Russia
*The Dispatch*

**A.** The article reports on a pre-election crackdown in Russia, and also mentions Poland foiling a Russian plot to kill a U.S. citizen, the U.S. losing a quarter of its Reaper drone fleet, and Count Binface's election results.

**B.** Russia is conducting a pre-election crackdown; Poland claims it foiled a Russian plot to kill a U.S. citizen in Warsaw, and the U.S. has lost roughly a quarter of its Reaper drone fleet.

<details>
<summary>What the models were given (35 words, teaser only)</summary>

> Plus: Poland says it foiled a Russian plot to kill a U.S. citizen in Warsaw, the U.S. loses roughly a quarter of its Reaper drone fleet, and Count Binface wins more votes than ever before.

</details>

---

### 42. When Japan buys yen, it unwinds a dangerous trade
*The Economist*

**A.** Japan, the world's largest carry trader, is beginning to exit its currency trading position, which could unwind a risky financial trade.

**B.** Japan, the world's biggest carry trader, is beginning to exit its position by buying yen, unwinding a dangerous trade.

<details>
<summary>What the models were given (10 words, teaser only)</summary>

> The world’s biggest carry trader begins to exit its position

</details>

---

### 43. Trump threatens to declare strait of Hormuz ‘territory of the United States’
*The Guardian US*

**A.** Trump threatened to declare the Strait of Hormuz, a critical global oil shipping chokepoint, as U.S. territory, though the seriousness and policy intent of the remark remain unclear.

**B.** Trump threatened to declare the Strait of Hormuz a U.S. territory, a crucial chokepoint for global oil trade, though the seriousness and policy implications are unclear.

<details>
<summary>What the models were given (94 words)</summary>

> Seriousness of remark made in New York on Friday, and whether it signaled new policy ​position, not clearDonald Trump has threatened to declare the strait of Hormuz as “a territory of the United States” as his administration struggles to conclude the war with Iran.During a speech at a police academy on Long Island, New York, on Friday, the US president said he would “pretty soon” designate the waterway – a crucial chokepoint for global trade, through which about a fifth of the world’s seaborne oil supplies typically pass – a US territory. Continue reading...

</details>

---

### 44. American missionary kidnapped in Niger freed after nine months
*The Guardian World*

**A.** American missionary Kevin Rideout, kidnapped in Niger in October, has been released and is in good health in the care of U.S. officials, according to his organization. He will soon be reunited with his family.

**B.** An American missionary kidnapped in Niger in October has been released after nine months and is reported to be in good health.

<details>
<summary>What the models were given (65 words)</summary>

> Kevin Rideout, who was abducted by unknown assailants in Niamey, said to be in good health and in care of US officialsAn American missionary who was kidnapped in Niger in October has been released, his organisation has said.Serving in Mission International said Kevin Rideout “is in good health in the care of US officials” and would soon be reunited with his extended family. Continue reading...

</details>

---

### 45. Judge lifts order on Somalia TPS, clearing way for deportations
*The Hill*

**A.** A federal judge lifted a stay blocking deportations of Somali nationals holding Temporary Protected Status, clearing the way for the Trump administration to proceed with deportations.

**B.** A federal judge lifted a stay in a case involving Somali holders of Temporary Protected Status, clearing the way for the Trump administration to begin deportations. The ruling reverses an earlier administrative stay.

<details>
<summary>What the models were given (55 words)</summary>

> A Massachusetts-based federal judge on Friday lifted her stay in a case brought by Somalian holders of Temporary Protected Status (TPS), clearing the way for the Trump administration to begin deporting them. The ruling from U.S. District Judge Allison Burroughs lifts an administrative stay she issued after the Supreme Court decided in a related case...

</details>

---

### 46. USS Abraham Lincoln controversy likely to loom over Pentagon
*The Hill Politics*

**A.** Congress and military families are scrutinizing conditions aboard the USS Abraham Lincoln following reports of low morale and poor conditions during its 265-day deployment, with the controversy expected to persist despite the aircraft carrier's return home.

**B.** Reports of low morale and poor conditions on the USS Abraham Lincoln have drawn scrutiny from Congress and families of service members during the Iran War, and the aircraft carrier's return home after a 265-day deployment is unlikely to end the controversy. The story matters because it highlights ongoing concerns about military conditions and oversight.

<details>
<summary>What the models were given (55 words)</summary>

> Reports of low morale and poor conditions on the USS Abraham Lincoln has prompted new scrutiny from Congress and the families of those serving during the Iran War, scrutiny unlikely to end even as the administration said the aircraft carrier was returning home on Friday. The return caps a grueling, more than 265-day deployment aboard...

</details>

---

### 47. Europe’s Broadcasters Want to Get Bigger. The Hard Part Is How.
*The Hollywood Reporter*

**A.** European broadcasters and production companies are seeking scale to compete with Netflix, Amazon, and YouTube in the global TV market, but face challenges in how to achieve that growth. The story matters as it reflects the shifting dynamics of the streaming industry.

**B.** European broadcasters and production companies are attempting to scale up and compete globally as Netflix, Amazon, and YouTube reshape the television market.

<details>
<summary>What the models were given (22 words, teaser only)</summary>

> As Netflix, Amazon and YouTube reshape the global TV market, European networks and production companies are chasing very different kinds of scale.

</details>

---

### 48. Has the Left Really Met Its Limit?
*The Intercept*

**A.** Democratic socialist Francesca Hong narrowly lost her Wisconsin gubernatorial primary to moderate David Crowley, marking a setback for the insurgent left, though progressives won other primary races including Lt. Gov. Peggy Flanagan's Senate victory in Minnesota.

**B.** The left's momentum hit a roadblock as democratic socialist Francesca Hong narrowly lost the Wisconsin governor primary to moderate David Crowley, though progressives saw wins in Minnesota. The story matters for understanding the internal power struggle within the Democratic Party.

<details>
<summary>What the models were given (450 words)</summary>

> The stunning momentum of the insurgent left hit a roadblock this week when democratic socialist Francesca Hong narrowly lost her primary race for Wisconsin governor to Milwaukee County Executive David Crowley, a moderate who garnered significant support from establishment Democrats.&nbsp;&nbsp; “The collective power of that Democratic establishment in a state, where there is a Democratic governor and there is a real Democratic Party establishment machine, all of that power was required to eke out a 0.4 percent victory in the party’s own primary against a previously unknown candidate running as a Democratic socialist,” The Lever’s David Sirota tells The Intercept Briefing.&nbsp; This week on the podcast, host Jessica Washington speaks with Sirota, founder and editor-in-chief of The Lever, about the primaries and how the left is building power within the Democratic Party.&nbsp; There were bright spots for progressives on Tuesday night. In Minnesota, progressive Lt. Gov. Peggy Flanagan defeated AIPAC-backed Rep. Angie Craig, D-Minn., in the Senate primary race. Craig had previously voted for the Laken Riley Act, which requires the federal government to detain people for certain crimes, including shoplifting and burglary. Flanagan, by contrast, told supporters on election night: “We need to rip ICE apart and stop them from terrorizing our communities.”&nbsp;&nbsp; “What you’re seeing now is, and I don’t want to call it a Democratic Tea Party, but it is certainly organizing and pressuring to take back power in an adversarial way from a set of forces to the left of the Democratic establishment,” says Sirota. “That is what’s new, and that is what’s driving the political dynamic now.”&nbsp; Washington and Sirota also discuss the intensifying democracy crisis unfolding in the United States. According to Sirota, it “stands on two pillars: concentrated executive power and the supremacy of money.” Sirota explores this theme and more, in the new season of his show, Master Plan: The Kingmakers. The podcast is all about how a once-fringe legal theory moved into the mainstream and transformed the power of the American presidency.&nbsp; For more, listen to the full conversation of The Intercept Briefing on Apple Podcasts, Spotify, YouTube, or wherever you listen. Transcript Jessica Washington: Welcome to The Intercept Briefing, I’m Jessica Washington, politics reporter at The Intercept.&nbsp; On Tuesday night, democratic socialist Francesca Hong narrowly lost her primary race for Wisconsin Governor to Milwaukee County Executive David Crowley. Hong lost by less than a percentage point. But her defeat was a blow to the anti-data center movement, which she had championed throughout her campaign, even as her detractors tried to make the race about her opinions on Thanksgiving and other major holidays.&nbsp;      Related  Francesca Hong’s Loss in Wisconsin is a Win for AI Data Centers — for Now    But the...

</details>

---

### 49. Samsung has new Galaxy headphones in the works
*The Verge*

**A.** Code found in Samsung's Galaxy Wearable app suggests the company is developing over-ear headphones called the 'Galaxy H1' to compete with Apple's AirPods Max, with a potential 2027 launch.

**B.** Code in Samsung's Galaxy Wearable app suggests the company is developing over-ear headphones called 'Galaxy H1' that could launch in 2027, potentially competing with Apple's AirPods Max. This matters as it signals Samsung's entry into a new product category.

<details>
<summary>What the models were given (56 words)</summary>

> Strings of code in Samsung's Galaxy Wearable app hint at an upcoming pair of over-ear headphones that could compete with the AirPods Max, SamMobile reports. Samsung's reportedly referring to the headphones as the "Galaxy H1," and SamMobile says they could launch sometime in 2027. That would make these the company's first pair of over-ear headphones [&#8230;]

</details>

---

### 50. Ex-credit union manager sentenced for defrauding elderly customers
*The Washington Times*

**A.** A former credit union branch manager was sentenced to three years in prison for defrauding elderly customers. The story matters as it highlights financial crimes against vulnerable populations.

**B.** A federal judge in Indiana sentenced a former credit union branch manager to three years in prison for defrauding elderly customers using her position.

<details>
<summary>What the models were given (26 words, teaser only)</summary>

> A federal judge in Indiana sentenced a former credit union branch manager to three years in prison for using her position to rip off elderly clientele.

</details>

---
