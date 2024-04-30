library(metagenomeSeq)
library(phyloseq)
library(funrar)
library(superheat)

MRexperiment_filter <- phyloseq_to_metagenomeSeq(ps)
MRexperiment_filter_prev <- filterData(MRexperiment_filter, present = n)
# Normalize (CSS) the table and export the count matrix (filtered)
p <- cumNormStatFast(MRexperiment_filter_prev) # Default value being used
MRexperiment_filter_prev <- cumNorm(MRexperiment_filter_prev, p = p)
MRexperiment_filter_prev_norm <- MRcounts(MRexperiment_filter_prev, norm = TRUE, log = TRUE)
## Build fitZIG models adjusting
# Groups and covariates
group <- pData(MRexperiment_filter_prev)$v1
depth <- pData(MRexperiment_filter_prev)$v2
# Normalization factor
norm.factor <- normFactors(MRexperiment_filter_prev)
norm.factor <- log2(norm.factor/median(norm.factor) + 1)
# Create the model
mod <- model.matrix(~ group + depth)
settings <- zigControl(maxit = 10, verbose = TRUE)
fit <- fitZig(obj = MRexperiment_filter_prev, mod = mod, useCSSoffset = FALSE, control = settings)
# Generate table of log fold change coefficients for each OTU, sorting by adjusted p-value
coefs <- MRcoefs(fit, coef = 2, group = 3, number = 500)
## Identify important taxa
# Remove taxa below threshold and reorder dataframe
summary(coefs$adjPvalues >= 0.01)
coefs$ASV <- rownames(coefs)
coefs <- coefs[which(coefs$adjPvalues < 0.01),]
coefs
cc.sig.asvs_filter_prev <- coefs[, c("ASV","group1", "pvalues", "adjPvalues")]
# Subset the matrix of CSS normalised and logged counts to the significant taxa
cc.sig.table <- MRexperiment_filter_prev_norm[cc.sig.asvs_filter_prev$ASV, ]
# Transpose the table
cc.sig.table <- t(cc.sig.table)
## Split into tables of cases and controls
# List both cases and control IDs
healthy_ids <- sample_data(subset_samples(ps, group %in% c("Healty")))$file_name
tumor_ids <- sample_data(subset_samples(ps, group %in% c("Tumor")))$file_name
healthy.table <- subset(cc.sig.table, rownames(cc.sig.table) %in% healthy_ids)
tumor.table <- subset(cc.sig.table, rownames(cc.sig.table) %in% tumor_ids)
## Calculating relative abundance data at an ASV level
# Read in the raw ASV table containing all samples (doesn't contain taxonomy)
cc.full.table <- as.matrix(otu_table(ps))
# Convert ASV table to relative abundance table by taking proportions of total
cc.abund.table <- make_relative(cc.full.table)*100
# Subset the abundance table to the significantly differentially abundant ASVs
cc.abund.table <- cc.abund.table[colnames(cc.sig.table), rownames(cc.sig.table)] ## This is for cases where not all samples were maintained for the significant ASVs selected
cc.abund.table <- t(cc.abund.table) ## Expected output table: sample in rows, asvs in columns
# Split the abundance table into the groups of comparisons
healthy.abundance <- subset(cc.abund.table, rownames(cc.abund.table) %in% healthy_ids)
tumor.abundance <- subset(cc.abund.table, rownames(cc.abund.table) %in% tumor_ids)
# Put the tables of abundance and table of normalised values in the same order
# healthy
ord1 <- match(colnames(healthy.abundance), colnames(healthy.table))
healthy.table <- healthy.table[,ord1]
ord2 <- match(rownames(healthy.abundance), rownames(healthy.table))
healthy.table <- healthy.table[ord2,]
# tumor
ord3 <- match(colnames(tumor.abundance), colnames(tumor.table))
tumor.table <- tumor.table[,ord3]
ord4 <- match(rownames(tumor.abundance), rownames(tumor.table))
tumor.table <- tumor.table[ord4,]
## Selecting OTUs that have a mean or median of at least 0.35% 
# Work with data frames
healthy.table <- as.data.frame(healthy.table)
tumor.table <- as.data.frame(tumor.table)
healthy.abundance <- as.data.frame(healthy.abundance)
tumor.abundance <- as.data.frame(tumor.abundance)
# Select only ASVs above threshold defined
cc.threshold <- c()
for (i in 1:length(healthy.table)) {
  cc.threshold[i] <- ifelse(median(healthy.abundance[,i]) > threshold | median(tumor.abundance[,i]) > threshold | mean(healthy.abundance[,i]) > threshold | mean(tumor.abundance[,i]) > threshold, names(healthy.table[i]), NA)
}
cc.threshold <- cc.threshold[!is.na(cc.threshold)]
cc.threshold
## Export table of mean and median values for all significant ASVs
# Determine mean and median values
cc.table <- cc.sig.asvs_filter_prev$ASV
mean.healthy.all <- c()
mean.tumor.all <- c()
median.healthy.all <- c()
median.tumor.all <- c()
for (i in cc.table) {
  mean.healthy.all[i] <- mean(healthy.abundance[,i])
  median.healthy.all[i] <- median(healthy.abundance[,i])
  mean.tumor.all[i] <- mean(tumor.abundance[,i])
  median.tumor.all[i] <- median(tumor.abundance[,i])
}
# Combine into table
cc.table.full <- data.frame(mean.healthy.all, median.healthy.all, mean.tumor.all, median.tumor.all)
# Subset the normalised logged counts to the ASVs above threshold
#cc.data <- cc.sig.table[,which(colnames(cc.sig.table) %in% cc.threshold)]  ### SKIP THIS PART WHEN ALL ASV WERE ABOVE THRESHOLD
# Transpose it
cc.data <- as.data.frame(t(cc.sig.table))
# Create a list of the fold-change coefficients to be plotted beside the heatmap
cc.coefs <- cc.sig.asvs_filter_prev[,1:2]
names(cc.coefs) <- c("ASV", "log2FC")
cc.coefs <- cc.coefs[which(cc.coefs$ASV %in% rownames(cc.data)),] ### SKIP THIS PART WHEN ASVs ARE THE SAME
# Make sure they're in the same order as the data
cc.coefs <- cc.coefs[match(rownames(cc.data), cc.coefs$ASV),]
# Include the genus level taxonomy in the rownames for nice image
taxa_asv <- as.data.frame(tax_table(ps))
taxa_asv$ASV <- rownames(taxa_asv)
cc.taxa <- taxa_asv[,c("ASV", "Order", "Family", "Genus", "Species")]
cc.taxa <- cc.taxa[which(cc.taxa$ASV %in% cc.coefs$ASV),]
# Same order as data
cc.taxa <- cc.taxa[match(cc.coefs$ASV, cc.taxa$ASV), ]
#cc.data <- t(cc.data) # Expected output ASV in rownames and sample in colnames
#rownames(cc.data) <- paste(cc.taxa$Family, "-", cc.taxa$Genus, "-", cc.taxa$Species, " (", rownames(cc.data), ") ", sep = "")
# Specify the variable to group/label samples by
healthy.abundance <- subset(cc.abund.table, rownames(cc.abund.table) %in% healthy_ids)
cc.data_ordered <- cc.data
tumor_pos <- which(sample_data(ps)$group %in% "Tumor")
healthy_pos <- which(sample_data(ps)$group %in% "Healthy")
all_pos <- c(healthy_pos, tumor_pos)
final_order <- paste(all_pos, collapse = ', ')
# Check indexes for both groups
cc.data_ordered <- cc.data_ordered[,c(order)]
cc.data_ordered <- cc.data_ordered[sort(rownames(cc.data_ordered), decreasing = TRUE),]
#rownames(cc.coefs) <- paste(cc.taxa$Family, "-", cc.taxa$Genus, "-", cc.taxa$Species, " (", rownames(cc.coefs), ") ", sep = "")
cc.coefs_ordered <- cc.coefs[sort(rownames(cc.coefs), decreasing = TRUE),]
groupcc <- ifelse(colnames(cc.data_ordered) %in% tumor_ids, "Tumor", "Healthy")
groupcc <- ifelse(colnames(cc.data_ordered) %in% healthy_ids, "Healthy", groupcc)
heatmap_css_DA <- superheat(cc.data_ordered,
                            title = "title",
                            # Sort and label by groupcc (in same order as samples)
                            membership.cols = groupcc,
                            # Order the rows and columns nicely by hierarchical clustering
                            #pretty.order.rows = TRUE,
                            pretty.order.cols = TRUE,
                            # Make the OTU labels smaller and align the text
                            left.label.size = 1,
                            left.label.text.size = 3,
                            left.label.text.alignment = "right",
                            # Change the colours of the labels
                            left.label.col = "White",
                            bottom.label.col = c("Grey50", "Grey"),
                            bottom.label.text.size = 3,
                            bottom.label.size = 0.1,
                            # Remove the black lines
                            grid.hline = FALSE,
                            grid.vline = FALSE,
                            # Add the log fold-change plot
                            yr = cc.coefs_ordered$log2FC,
                            yr.axis.name = "log2 fold change",
                            yr.axis.name.size = 9,
                            yr.axis.size = 9,
                            yr.plot.size = 0.15,
                            # Heatmap scheme
                            heat.col.scheme = "red",
                            heat.lim = c(-0, 20),
                            legend.text.size = 9)