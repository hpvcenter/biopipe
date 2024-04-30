library(phyloseq)
library(biomformat)
library(ggplot2)
# Load data
ps <- import_biom("file.biom")
metadata <- read.table("metadata.txt", sep = "\t", header = TRUE)
ps <- prune_taxa(taxa_sums(ps) > 0, ps)
sample_data(ps) <- metadata # Sample names have to be the same than in the ps object
# Organize taxa names
tax_table(ps) <- substring(tax_table(ps), 4)
colnames(tax_table(ps)) <- c("Kingdom", "Phylum", "Class", "Order", "Family", "Genus", "Species")
tax_table(ps)[tax_table(ps)==""]<-NA
# Subset for taxonomic level of interest
ps_taxlevel <- subset_taxa(ps, Taxlevel != "NA") # Change taxlevel by the taxonomic level of interest
# Split for bacteria, virus, or fungi
bact_taxlevel <- subset_taxa(ps_taxlevel, Kingdom == "Bacteria")
virus_taxlevel <- subset_taxa(ps_taxlevel, Kingdom == "Viruses")
fungi_taxlevel <- subset_taxa(ps_taxlevel, Kingdom == "Eukaryota")
# Filter data (custom)
# Example for bacteria 0.1% relative abundance in at least one sample
bact_taxlevel_relab <- transform_sample_counts(bact_taxlevel, function(x) x / sum(x) )
bact_taxlevel_relab_filt <- filter_taxa(bact_taxlevel_relab, function(x) max(x) >= 0.001, TRUE)
bact_taxlevel_filt <- prune_taxa(taxa_names(bact_taxlevel_relab_filt), bact_taxlevel)
# Alpha diversity
# Example for bacteria
rare_level_bact <- min(sample_sums(bact_taxlevel)) # Check rarefaction level, if not too small. Otherwise adjust sample set to compute alpha diversity.
bact_taxlevel_rare <- rarefy_even_depth(bact_taxlevel, rngseed = 20170215)
alpha_diversity <- estimate_richness(bact_taxlevel_rare, measures = c("Index of interest")) # Substitute by the index of interest
plot_richness(bact_taxlevel_rare, color = "Group", x = "Group", measures = c("Index of interest")) +
  geom_boxplot(aes(fill = Group)) 
# Beta diversity
# Example for bacteria
ord_bc_bact_taxlevel <- ordinate(bact_taxlevel, method = "MDS", distance = "bray") # Method and distance are customizable depending on the index of interest and representation
anosim_bc_bact_taxlevel_group <- anosim(phyloseq::distance(bact_taxlevel, method="bray"), sample_data(bact_taxlevel) %>% pull(Group), permutations = 1000) # Same than previous line
plot_ordination(bact_taxlevel, ord_bc_bact_taxlevel, color="Group", title="Bacteria - Bray-Curtis") + stat_ellipse(aes(group=Group))